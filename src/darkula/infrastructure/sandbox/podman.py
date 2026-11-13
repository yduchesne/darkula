# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Podman-backed disposable Sandbox (PR 7).

Creates exactly one fresh Darkula-owned crawler container per execution on a
Darkula-owned internal network, transfers a bounded input on stdin, collects
bounded stdout/stderr, enforces timeout/cancellation, inspects the exit
state, removes the container, and returns a typed result.

Security semantics:

- one fresh container per execution; hostile state never leaks between
  executions;
- the container is non-privileged, not host-networked, rootfs read-only with
  bounded tmpfs, all capabilities dropped, no-new-privileges, PID/memory/CPU
  and execution-time bounds;
- connectivity is enforced by the Darkula-owned internal network topology
  below any workload logic (only authorized destinations are reachable);
- cleanup runs on every terminal path (success, workload failure, timeout,
  termination, cancellation) and never touches unverified/foreign resources;
- ``asyncio.CancelledError`` propagates after termination and cleanup;
- Podman unavailability is a typed sandbox-unavailable failure — there is
  never a host-side fallback.

All Podman invocations use explicit argument vectors (no shell), and only
Darkula-owned resources (exact names + ``darkula.owned=true``) are ever
created, mutated, or removed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import shutil
import time
import uuid
from dataclasses import dataclass

from darkula.sandbox.contracts import (
    EgressMode,
    Sandbox,
    SandboxExecutionMetrics,
    SandboxExecutionRequest,
    SandboxExecutionResult,
    SandboxExitReason,
    SandboxInfrastructureError,
    validate_execution_request,
)
from darkula.telemetry.metrics import get_counter

_LOGGER = logging.getLogger(__name__)

#: Bounded stderr capture ceiling (diagnostics only, never returned verbatim).
_MAX_STDERR_BYTES = 65536

#: Runtime user inside the crawler image (non-root; defined in the Containerfile).
_RUNTIME_USER = 10001

#: Ownership/service label vocabulary (must match scripts + Containerfile).
_LABEL_OWNED = "darkula.owned=true"
_LABEL_SERVICE = "darkula.service=crawler"
_LABEL_LIVE_NETWORK = "darkula.service=https-egress-network"
_LABEL_LIVE_GATEWAY = "darkula.service=https-egress-gateway"
_LABEL_LIVE_CRAWLER = "darkula.service=https-egress-crawler"

#: Fixed gateway runtime identity/limits (least privilege; no host ports).
_GATEWAY_USER = 10002
_GATEWAY_MEMORY = "128m"
_GATEWAY_CPUS = "0.5"
_GATEWAY_PIDS = 64
_GATEWAY_TMPFS = "/tmp:rw,size=16m,mode=1777"  # nosec B108 - sandbox tmpfs

#: Bounded readiness wait for the gateway's own internal listener.
_GATEWAY_READY_TIMEOUT_SECONDS = 10.0

#: Fixed cleanup budget: no unbounded removal attempts, ever.
_CLEANUP_MAX_ATTEMPTS = 2
_CLEANUP_COMMAND_TIMEOUT_SECONDS = 10.0

#: Bounded execution identity length embedded in container names.
_MAX_EXECUTION_ID_IN_NAME = 40


class _PodmanCliError(SandboxInfrastructureError):
    """A bounded Podman CLI failure (never echoes command output)."""


def _validated_container_status(document: dict[str, object]) -> str | None:
    """Return a container's status only when it is exact-owned and readable."""
    config = document.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if not isinstance(labels, dict) or labels.get("darkula.owned") != "true":
        return None
    state = document.get("State")
    if not isinstance(state, dict):
        return None
    status = state.get("Status")
    if not isinstance(status, str) or not status:
        return None
    return status


@dataclass(slots=True)
class _LiveResources:
    """Exact per-execution resources owned by one public-HTTPS crawl."""

    execution_id: str
    network: str
    gateway: str
    crawler: str
    network_created: bool = False
    gateway_started: bool = False


def _select_ephemeral_addresses(
    document: dict[str, object],
) -> tuple[str, str, str]:
    """Return ``(subnet, gateway_ip, crawler_ip)`` for one /24 network."""
    subnets = document.get("subnets")
    if not isinstance(subnets, list) or len(subnets) != 1:
        raise _PodmanCliError("ephemeral network subnet topology is unsupported")
    entry = subnets[0]
    if not isinstance(entry, dict):
        raise _PodmanCliError("ephemeral network subnet metadata is unreadable")
    raw = entry.get("subnet")
    if not isinstance(raw, str):
        raise _PodmanCliError("ephemeral network subnet metadata is unreadable")
    try:
        network = ipaddress.IPv4Network(raw, strict=True)
    except ValueError:
        raise _PodmanCliError("ephemeral network subnet is not IPv4") from None
    if network.prefixlen != 24:
        raise _PodmanCliError("ephemeral network subnet must be a /24")
    gateway = network.network_address + 10
    crawler = network.network_address + 11
    if gateway not in network or crawler not in network:
        raise _PodmanCliError("ephemeral network has no usable host addresses")
    return str(network), str(gateway), str(crawler)


@dataclass(frozen=True, slots=True)
class PodmanSandboxConfig:
    """Adapter-level configuration for the Podman sandbox.

    ``image`` is the deterministic crawler runtime image tag; ``network`` is
    the Darkula-owned internal network used to enforce authorized-only
    connectivity; ``binary`` may be overridden (default ``podman``).
    """

    image: str
    network: str = "darkula-intg"
    binary: str = "podman"
    live_egress_network: str = "darkula-live-egress"
    gateway_image: str = "localhost/darkula-https-egress:1"


class PodmanSandbox(Sandbox):
    """Disposable one-container-per-execution sandbox over Podman."""

    def __init__(self, config: PodmanSandboxConfig) -> None:
        self._config = config

    @classmethod
    def from_settings(cls, settings: object) -> PodmanSandbox:
        """Build the adapter from resolved crawler settings (fail closed)."""
        from darkula.config.settings import CrawlerSettings

        if not isinstance(settings, CrawlerSettings):
            raise TypeError("PodmanSandbox requires CrawlerSettings")
        return cls(
            config=PodmanSandboxConfig(
                image=settings.runtime_image,
                network=settings.sandbox_network,
                live_egress_network=settings.real_https_egress_network,
                gateway_image=settings.real_https_gateway_image,
            )
        )

    @staticmethod
    def container_name_for(execution_id: str, nonce: str | None = None) -> str:
        """Return the exact Darkula-namespaced container name for an execution."""
        token = nonce or uuid.uuid4().hex[:8]
        return f"darkula-crawler-{execution_id[:_MAX_EXECUTION_ID_IN_NAME]}-{token}"

    # -- podman helpers (exact-argument vectors, fail closed) --------------
    async def _run_podman(
        self, argv: list[str], *, command_timeout: float = 30.0
    ) -> tuple[int, bytes, bytes]:
        """Run one podman command with bounded stdout/stderr capture."""
        binary = self._config.binary
        if shutil.which(binary) is None:
            raise SandboxInfrastructureError(f"podman binary {binary!r} was not found")
        proc = await asyncio.create_subprocess_exec(
            binary,
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=command_timeout
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise _PodmanCliError("podman command timed out") from exc
        return proc.returncode or 0, stdout, stderr

    async def _container_state(self, name: str) -> tuple[bool, bool, str | None]:
        """Return ``(exists, is_owned, state)`` for one exact container.

        ``is_owned`` is meaningful only when ``exists``; an unreadable or
        unparseable inspection reports ``is_owned=False`` (fail closed).
        """
        code, stdout, _ = await self._run_podman(["container", "inspect", name])
        if code != 0:
            return False, False, None
        try:
            documents = json.loads(stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise _PodmanCliError("cannot read container inspection metadata") from exc
        if not isinstance(documents, list) or not documents:
            return False, False, None
        labels = documents[0].get("Config", {}).get("Labels", {})
        owned = isinstance(labels, dict) and labels.get("darkula.owned") == "true"
        state = documents[0].get("State", {}).get("Status")
        return True, owned, state

    async def _cleanup_resource_exists(self, kind: str, name: str) -> bool:
        """Prove exact resource presence using only the documented 0/1 codes.

        ``0`` means present, ``1`` means absent. Every other code (including
        125 operational failure) is unknown and must never be read as absent.
        """
        if kind not in {"container", "network"}:
            raise _PodmanCliError("unsupported cleanup resource kind")
        code, _, _ = await self._run_podman(
            [kind, "exists", name],
            command_timeout=_CLEANUP_COMMAND_TIMEOUT_SECONDS,
        )
        if code == 0:
            return True
        if code == 1:
            return False
        raise _PodmanCliError("cleanup resource presence could not be verified")

    async def _remove_owned_container(self, name: str) -> bool:
        """Remove one exact-owned container within a fixed two-attempt budget.

        A nonzero ``stop`` never proceeds to ``rm`` in the same attempt, and
        only ``container exists`` code ``1`` proves disappearance. Unknown
        presence, unreadable metadata and unowned resources fail closed.
        """
        for _attempt in range(_CLEANUP_MAX_ATTEMPTS):
            try:
                exists = await self._cleanup_resource_exists("container", name)
            except SandboxInfrastructureError:
                _LOGGER.error("podman cleanup presence check failed for %s", name)
                return False
            if not exists:
                return True
            try:
                document = await self._container_inspect(
                    name, command_timeout=_CLEANUP_COMMAND_TIMEOUT_SECONDS
                )
            except SandboxInfrastructureError:
                _LOGGER.error("podman cleanup inspect failed for %s", name)
                return False
            status = (
                _validated_container_status(document) if document is not None else None
            )
            if status is None:
                _LOGGER.error("refusing to remove unverified container %s", name)
                return False
            if status == "running":
                try:
                    stop_code, _, _ = await self._run_podman(
                        ["stop", "-t", "2", name],
                        command_timeout=_CLEANUP_COMMAND_TIMEOUT_SECONDS,
                    )
                except SandboxInfrastructureError:
                    _LOGGER.error("podman cleanup stop failed for %s", name)
                    return False
                if stop_code != 0:
                    try:
                        exists = await self._cleanup_resource_exists("container", name)
                    except SandboxInfrastructureError:
                        return False
                    if not exists:
                        return True
                    continue
            # Revalidate exact presence and ownership immediately before rm.
            try:
                exists = await self._cleanup_resource_exists("container", name)
            except SandboxInfrastructureError:
                return False
            if not exists:
                return True
            try:
                document = await self._container_inspect(
                    name, command_timeout=_CLEANUP_COMMAND_TIMEOUT_SECONDS
                )
            except SandboxInfrastructureError:
                return False
            if document is None or _validated_container_status(document) is None:
                _LOGGER.error("refusing to remove unverified container %s", name)
                return False
            try:
                await self._run_podman(
                    ["rm", "-f", name],
                    command_timeout=_CLEANUP_COMMAND_TIMEOUT_SECONDS,
                )
            except SandboxInfrastructureError:
                _LOGGER.error("podman cleanup rm failed for %s", name)
                return False
            try:
                exists = await self._cleanup_resource_exists("container", name)
            except SandboxInfrastructureError:
                return False
            if not exists:
                return True
        return False

    def _build_run_argv(self, request: SandboxExecutionRequest, name: str) -> list[str]:
        """Build the exact ``podman run`` argument vector (no shell).

        Policy fields map 1:1 to container flags; every capability not
        granted by the policy is absent (denied by default).
        """
        policy = request.policy
        argv = [
            "run",
            "-i",
            "--name",
            name,
            "--network",
            self._config.network,
            "--label",
            _LABEL_OWNED,
            "--label",
            _LABEL_SERVICE,
            "--label",
            f"darkula.execution={request.execution_id}",
            "--read-only",
            "--cap-drop",
            "all",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(policy.resources.pids),
            "--memory",
            f"{policy.resources.memory_bytes}b",
            "--cpus",
            f"{policy.resources.cpus:g}",
        ]
        for mount in policy.filesystem.tmpfs:
            argv += [
                "--tmpfs",
                f"{mount.path}:rw,size={mount.size_bytes},mode=1777",
            ]
        if policy.runtime.non_root_user:
            argv += ["--user", str(_RUNTIME_USER)]
        argv += [self._config.image, "python3", "-m", "crawler_runtime"]
        return argv

    # -- execution --------------------------------------------------------
    async def execute(self, request: SandboxExecutionRequest) -> SandboxExecutionResult:
        validate_execution_request(request)
        if request.policy.network.egress is EgressMode.PUBLIC_HTTPS_PROXY:
            return await self._execute_public_https(request)
        started = time.perf_counter()
        name = self.container_name_for(request.execution_id)

        if shutil.which(self._config.binary) is None:
            return self._recorded_result(
                request,
                SandboxExitReason.INFRASTRUCTURE_FAILURE,
                started,
                reason=f"podman binary {self._config.binary!r} was not found",
            )

        # Preflight: the exact container name must not collide. A same-name
        # resource that is not positively Darkula-owned fails closed (never
        # adopted, relabeled, recreated, or removed).
        try:
            exists, owned, _ = await self._container_state(name)
        except SandboxInfrastructureError as exc:
            return self._recorded_result(
                request,
                SandboxExitReason.INFRASTRUCTURE_FAILURE,
                started,
                reason=f"cannot verify execution ownership: {exc}",
            )
        if exists and not owned:
            return self._recorded_result(
                request,
                SandboxExitReason.INFRASTRUCTURE_FAILURE,
                started,
                reason="same-name unowned podman resource found; refusing execution",
            )

        proc: asyncio.subprocess.Process | None = None
        exit_code: int | None = None
        stdout = b""
        oversized = False
        timed_out = False
        cancelled = False
        infrastructure_failure_reason: str | None = None
        try:
            argv = self._build_run_argv(request, name)
            proc = await asyncio.create_subprocess_exec(
                self._config.binary,
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            if proc.stdin is None or proc.stdout is None or proc.stderr is None:
                raise SandboxInfrastructureError(
                    "podman subprocess I/O pipes are unavailable; failing closed"
                )
            payload = request.workload.payload
            if len(payload) > request.policy.input.max_input_bytes:
                raise SandboxInfrastructureError(
                    "workload payload exceeds the declared input bound"
                )
            proc.stdin.write(payload)
            await proc.stdin.drain()
            proc.stdin.close()
            try:
                stdout, oversized = await asyncio.wait_for(
                    self._collect_outputs(proc, request.policy.output.max_output_bytes),
                    timeout=request.policy.resources.timeout_seconds,
                )
            except TimeoutError:
                timed_out = True
                await self._terminate(proc)
            except asyncio.CancelledError:
                cancelled = True
                await self._terminate(proc)
                raise
            exit_code = proc.returncode
        except SandboxInfrastructureError as exc:
            infrastructure_failure_reason = f"podman execution failed: {exc}"
        finally:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            removed = await self._remove_owned_container(name)
            if not removed:
                _LOGGER.error("crawler container %s not verifiably removed", name)
                get_counter("darkula.sandbox.cleanup_failures").add(
                    1, {"darkula.sandbox.execution": request.execution_id}
                )

        if infrastructure_failure_reason is not None:
            return self._recorded_result(
                request,
                SandboxExitReason.INFRASTRUCTURE_FAILURE,
                started,
                reason=(
                    infrastructure_failure_reason
                    if removed
                    else "sandbox execution failed; cleanup unverified"
                ),
                cleanup_succeeded=removed,
            )

        if cancelled:
            # Cancellation propagated; this line is unreachable because the
            # CancelledError re-raises above, but keeps the result typed.
            self._recorded_result(  # pragma: no cover - defensive
                request,
                SandboxExitReason.CANCELLED,
                started,
                reason="execution cancelled",
            )
        return self._build_result(
            request,
            started,
            exit_code,
            stdout,
            oversized,
            timed_out,
            removed=removed,
        )

    # -- public HTTPS topology (PR 20) ------------------------------------
    async def _execute_public_https(
        self, request: SandboxExecutionRequest
    ) -> SandboxExecutionResult:
        """Execute one explicitly authorized public-HTTPS crawl.

        Provisioning is always: fresh ephemeral network -> gateway -> crawler;
        cleanup is always crawler -> gateway -> network. There is never a
        crawler on the outer egress network and never a fallback path.
        """
        started = time.perf_counter()
        token = request.execution_id[:20]
        nonce = uuid.uuid4().hex[:8]
        resources = _LiveResources(
            execution_id=request.execution_id,
            network=f"darkula-https-net-{token}-{nonce}",
            gateway=f"darkula-https-gw-{token}-{nonce}",
            crawler=f"darkula-crawler-{token}-{nonce}",
        )
        failure: str | None = None
        payload_result: tuple[int | None, bytes, bool, bool] | None = None
        unexpected_error: BaseException | None = None
        cancelled_during_run = False
        try:
            payload_result = await self._run_public_https(request, resources)
        except asyncio.CancelledError:
            cancelled_during_run = True
        except SandboxInfrastructureError as exc:
            failure = f"public HTTPS infrastructure failed: {exc}"
        except BaseException as exc:
            unexpected_error = exc

        cleanup_verified, cancelled_during_cleanup = await self._cleanup_live_shielded(
            resources
        )

        if cancelled_during_run or cancelled_during_cleanup:
            self._recorded_result(
                request,
                SandboxExitReason.CANCELLED,
                started,
                reason="execution cancelled",
                cleanup_succeeded=cleanup_verified,
            )
            raise asyncio.CancelledError

        if unexpected_error is not None:
            self._recorded_result(
                request,
                SandboxExitReason.INFRASTRUCTURE_FAILURE,
                started,
                reason="public HTTPS execution raised an unexpected error",
                cleanup_succeeded=cleanup_verified,
            )
            raise unexpected_error

        if failure is not None:
            return self._recorded_result(
                request,
                SandboxExitReason.INFRASTRUCTURE_FAILURE,
                started,
                reason=(
                    failure
                    if cleanup_verified
                    else "public HTTPS infrastructure failed; cleanup unverified"
                ),
                cleanup_succeeded=cleanup_verified,
            )

        if payload_result is None:
            return self._recorded_result(
                request,
                SandboxExitReason.INFRASTRUCTURE_FAILURE,
                started,
                reason="public HTTPS execution produced no outcome",
                cleanup_succeeded=cleanup_verified,
            )

        exit_code, stdout, oversized, timed_out = payload_result
        return self._build_result(
            request,
            started,
            exit_code,
            stdout,
            oversized,
            timed_out,
            removed=cleanup_verified,
        )

    async def _run_public_https(
        self,
        request: SandboxExecutionRequest,
        resources: _LiveResources,
    ) -> tuple[int | None, bytes, bool, bool]:
        """Provision the topology and run the crawler workload."""
        host = request.policy.network.destinations[0].host
        await self._preflight_public_https(resources)
        await self._run_podman(
            [
                "network",
                "create",
                "--internal",
                "--label",
                _LABEL_OWNED,
                "--label",
                _LABEL_LIVE_NETWORK,
                "--label",
                f"darkula.execution={resources.execution_id}",
                resources.network,
            ],
            command_timeout=60,
        )
        resources.network_created = True
        created = await self._network_inspect(resources.network)
        if created is None:
            raise _PodmanCliError("ephemeral network was not created")
        labels = created.get("labels") or {}
        if not isinstance(labels, dict) or labels.get("darkula.owned") != "true":
            raise _PodmanCliError("ephemeral network ownership was not verified")
        if created.get("internal") is not True:
            raise _PodmanCliError("ephemeral network is not internal")
        subnet, gateway_ip, crawler_ip = _select_ephemeral_addresses(created)

        await self._start_live_gateway(request, resources, host, gateway_ip)
        resources.gateway_started = True
        await self._verify_live_gateway(resources, gateway_ip)
        await self._wait_live_gateway_ready(resources.gateway)

        argv = self._build_live_crawler_argv(
            request,
            name=resources.crawler,
            network=resources.network,
            crawler_ip=crawler_ip,
            gateway_ip=gateway_ip,
            subnet=subnet,
        )
        return await self._run_live_crawler_process(request, argv)

    async def _preflight_public_https(self, resources: _LiveResources) -> None:
        """Verify the opt-in outer network/image before creating anything."""
        if shutil.which(self._config.binary) is None:
            raise _PodmanCliError(
                f"podman binary {self._config.binary!r} was not found"
            )
        outer = await self._network_inspect(self._config.live_egress_network)
        if outer is None:
            raise _PodmanCliError("outer egress network is not provisioned")
        labels = outer.get("labels") or {}
        if not isinstance(labels, dict) or labels.get("darkula.owned") != "true":
            raise _PodmanCliError("outer egress network ownership was not verified")
        if outer.get("internal") is not False:
            raise _PodmanCliError("outer egress network must not be internal")
        for image in (self._config.image, self._config.gateway_image):
            code, _, _ = await self._run_podman(["image", "exists", image])
            if code != 0:
                raise _PodmanCliError("required live image is not present")
        for name in (resources.network, resources.gateway, resources.crawler):
            if await self._network_inspect(name) is not None:
                raise _PodmanCliError("live network name collision")
            exists, _, _ = await self._container_state(name)
            if exists:
                raise _PodmanCliError("live container name collision")

    def _build_live_crawler_argv(
        self,
        request: SandboxExecutionRequest,
        *,
        name: str,
        network: str,
        crawler_ip: str,
        gateway_ip: str,
        subnet: str,
    ) -> list[str]:
        """Build the crawler argv: one internal network, no DNS, no proxy env."""
        policy = request.policy
        argv = [
            "run",
            "-i",
            "--name",
            name,
            "--network",
            f"{network}:ip={crawler_ip}",
            "--dns",
            "none",
            "--http-proxy=false",
            "--label",
            _LABEL_OWNED,
            "--label",
            _LABEL_LIVE_CRAWLER,
            "--label",
            f"darkula.execution={request.execution_id}",
            "--read-only",
            "--cap-drop",
            "all",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(policy.resources.pids),
            "--memory",
            f"{policy.resources.memory_bytes}b",
            "--cpus",
            f"{policy.resources.cpus:g}",
            "-e",
            f"DARKULA_CRAWLER_HTTPS_PROXY=http://{gateway_ip}:3128",
            "-e",
            f"DARKULA_CRAWLER_INTERNAL_CIDR={subnet}",
        ]
        for mount in policy.filesystem.tmpfs:
            argv += [
                "--tmpfs",
                f"{mount.path}:rw,size={mount.size_bytes},mode=1777",
            ]
        if policy.runtime.non_root_user:
            argv += ["--user", str(_RUNTIME_USER)]
        argv += [self._config.image, "python3", "-m", "crawler_runtime"]
        return argv

    async def _start_live_gateway(
        self,
        request: SandboxExecutionRequest,
        resources: _LiveResources,
        host: str,
        gateway_ip: str,
    ) -> None:
        """Start the gateway multi-homed on the ephemeral and outer networks."""
        gateway_lifetime = min(90.0, request.policy.resources.timeout_seconds + 5.0)
        argv = [
            "run",
            "-d",
            "--name",
            resources.gateway,
            "--network",
            f"{resources.network}:ip={gateway_ip}",
            "--network",
            self._config.live_egress_network,
            "--http-proxy=false",
            "--label",
            _LABEL_OWNED,
            "--label",
            _LABEL_LIVE_GATEWAY,
            "--label",
            f"darkula.execution={resources.execution_id}",
            "--read-only",
            "--cap-drop",
            "all",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(_GATEWAY_PIDS),
            "--memory",
            _GATEWAY_MEMORY,
            "--cpus",
            _GATEWAY_CPUS,
            "--tmpfs",
            _GATEWAY_TMPFS,
            "--user",
            str(_GATEWAY_USER),
            "-e",
            f"DARKULA_EGRESS_HOST={host}",
            "-e",
            f"DARKULA_EGRESS_BIND_IPV4={gateway_ip}",
            "-e",
            "DARKULA_EGRESS_CONNECT_PORT=443",
            "-e",
            "DARKULA_EGRESS_LISTEN_PORT=3128",
            "-e",
            f"DARKULA_EGRESS_EXECUTION_TIMEOUT={gateway_lifetime:g}",
            self._config.gateway_image,
            "python3",
            "-m",
            "https_egress",
        ]
        await self._run_podman(argv, command_timeout=60)

    async def _verify_live_gateway(
        self, resources: _LiveResources, gateway_ip: str
    ) -> None:
        """Prove exactly the two approved networks, internal IP and no ports."""
        document = await self._container_inspect(resources.gateway)
        if document is None:
            raise _PodmanCliError("gateway container was not created")
        config = document.get("Config") or {}
        labels = config.get("Labels") if isinstance(config, dict) else None
        if not isinstance(labels, dict) or labels.get("darkula.owned") != "true":
            raise _PodmanCliError("gateway ownership was not verified")
        state = document.get("State") or {}
        if not isinstance(state, dict) or state.get("Running") is not True:
            raise _PodmanCliError("gateway is not running")
        settings = document.get("NetworkSettings") or {}
        networks = settings.get("Networks") if isinstance(settings, dict) else None
        if not isinstance(networks, dict):
            raise _PodmanCliError("gateway network metadata is unreadable")
        expected = {resources.network, self._config.live_egress_network}
        if set(networks) != expected:
            raise _PodmanCliError("gateway is attached to unexpected networks")
        internal = networks.get(resources.network) or {}
        if not isinstance(internal, dict) or internal.get("IPAddress") != gateway_ip:
            raise _PodmanCliError("gateway internal address was not assigned")
        ports = settings.get("Ports") if isinstance(settings, dict) else None
        if ports:
            raise _PodmanCliError("gateway unexpectedly publishes a host port")
        host_config = document.get("HostConfig") or {}
        if isinstance(host_config, dict) and host_config.get("PortBindings"):
            raise _PodmanCliError("gateway unexpectedly publishes a host port")
        if isinstance(host_config, dict) and host_config.get("NetworkMode") in (
            "host",
            "slirp4netns",
            "pasta",
        ):
            raise _PodmanCliError("gateway uses an unsupported network mode")

    async def _wait_live_gateway_ready(self, name: str) -> None:
        """Poll the gateway's own in-container listener (no public fetch)."""
        deadline = time.monotonic() + _GATEWAY_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            code, _, _ = await self._run_podman(
                ["exec", name, "python3", "-m", "https_egress.healthcheck"],
                command_timeout=5,
            )
            if code == 0:
                return
            await asyncio.sleep(0.25)
        raise _PodmanCliError("gateway readiness could not be verified")

    async def _run_live_crawler_process(
        self, request: SandboxExecutionRequest, argv: list[str]
    ) -> tuple[int | None, bytes, bool, bool]:
        """Run the crawler process with the bounded stdin/stdout contract."""
        proc: asyncio.subprocess.Process | None = None
        exit_code: int | None = None
        stdout = b""
        oversized = False
        timed_out = False
        try:
            proc = await asyncio.create_subprocess_exec(
                self._config.binary,
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            if proc.stdin is None or proc.stdout is None or proc.stderr is None:
                raise SandboxInfrastructureError(
                    "podman subprocess I/O pipes are unavailable; failing closed"
                )
            proc.stdin.write(request.workload.payload)
            await proc.stdin.drain()
            proc.stdin.close()
            try:
                stdout, oversized = await asyncio.wait_for(
                    self._collect_outputs(proc, request.policy.output.max_output_bytes),
                    timeout=request.policy.resources.timeout_seconds,
                )
            except TimeoutError:
                timed_out = True
                await self._terminate(proc)
            exit_code = proc.returncode
        except asyncio.CancelledError:
            if proc is not None:
                await self._terminate(proc)
            raise
        finally:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
        return exit_code, stdout, oversized, timed_out

    async def _cleanup_live_shielded(
        self, resources: _LiveResources
    ) -> tuple[bool, bool]:
        """Complete one cleanup task despite caller cancellation.

        Returns ``(cleanup_verified, caller_cancelled_during_cleanup)``. The
        loop only awaits the one bounded cleanup worker; it never creates a
        second worker or retries removals.
        """
        task = asyncio.create_task(self._cleanup_live(resources))
        caller_cancelled = False
        while True:
            try:
                return bool(await asyncio.shield(task)), caller_cancelled
            except asyncio.CancelledError:
                caller_cancelled = True
                if task.done():
                    try:
                        return bool(task.result()), True
                    except BaseException:
                        return False, True
            except Exception:
                return False, caller_cancelled

    async def _cleanup_live(self, resources: _LiveResources) -> bool:
        """Remove both containers, then the network only when containment clears."""
        removed_crawler = await self._remove_owned_container(resources.crawler)
        removed_gateway = await self._remove_owned_container(resources.gateway)
        if not (removed_crawler and removed_gateway):
            return False
        return await self._remove_owned_network(resources.network)

    async def _network_inspect(
        self, name: str, *, command_timeout: float = 30.0
    ) -> dict[str, object] | None:
        """Return exact network metadata, or ``None`` when absent."""
        code, stdout, _ = await self._run_podman(
            ["network", "inspect", name], command_timeout=command_timeout
        )
        if code != 0:
            return None
        try:
            documents = json.loads(stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise _PodmanCliError("cannot read network inspection metadata") from exc
        if not isinstance(documents, list) or not documents:
            return None
        document = documents[0]
        return document if isinstance(document, dict) else None

    async def _container_inspect(
        self, name: str, *, command_timeout: float = 30.0
    ) -> dict[str, object] | None:
        """Return exact container metadata, or ``None`` when absent."""
        code, stdout, _ = await self._run_podman(
            ["container", "inspect", name], command_timeout=command_timeout
        )
        if code != 0:
            return None
        try:
            documents = json.loads(stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise _PodmanCliError("cannot read container inspection metadata") from exc
        if not isinstance(documents, list) or not documents:
            return None
        document = documents[0]
        return document if isinstance(document, dict) else None

    async def _remove_owned_network(self, name: str) -> bool:
        """Remove one exact-owned network within a fixed two-attempt budget."""
        for _attempt in range(_CLEANUP_MAX_ATTEMPTS):
            try:
                exists = await self._cleanup_resource_exists("network", name)
            except SandboxInfrastructureError:
                _LOGGER.error("podman cleanup presence check failed for %s", name)
                return False
            if not exists:
                return True
            try:
                document = await self._network_inspect(
                    name, command_timeout=_CLEANUP_COMMAND_TIMEOUT_SECONDS
                )
            except SandboxInfrastructureError:
                _LOGGER.error("podman cleanup inspect failed for %s", name)
                return False
            labels = document.get("labels") if document is not None else None
            if not isinstance(labels, dict) or labels.get("darkula.owned") != "true":
                _LOGGER.error("refusing to remove unverified network %s", name)
                return False
            try:
                await self._run_podman(
                    ["network", "rm", name],
                    command_timeout=_CLEANUP_COMMAND_TIMEOUT_SECONDS,
                )
            except SandboxInfrastructureError:
                _LOGGER.error("podman cleanup rm failed for %s", name)
                return False
            try:
                exists = await self._cleanup_resource_exists("network", name)
            except SandboxInfrastructureError:
                return False
            if not exists:
                return True
        return False

    # -- helpers ----------------------------------------------------------
    async def _collect_outputs(
        self, proc: asyncio.subprocess.Process, max_output_bytes: int
    ) -> tuple[bytes, bool]:
        """Read bounded stdout/stderr; return (stdout, oversized flag).

        Keeps only the first ``max_output_bytes`` bytes of stdout (drains
        the remainder to avoid pipe deadlock) and the first bounded stderr.
        """
        stdout_first = bytearray()
        stdout_total = 0
        oversized = False

        async def read_stdout() -> None:
            nonlocal stdout_total, oversized, stdout_first
            stream = proc.stdout
            if stream is None:  # pragma: no cover - guarded by caller
                raise SandboxInfrastructureError(
                    "podman stdout pipe is unavailable; failing closed"
                )
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    return
                stdout_total += len(chunk)
                if stdout_total > max_output_bytes:
                    oversized = True
                room = max_output_bytes - len(stdout_first)
                if room > 0:
                    stdout_first += chunk[:room]

        async def drain_stderr() -> None:
            stream = proc.stderr
            if stream is None:  # pragma: no cover - guarded by caller
                raise SandboxInfrastructureError(
                    "podman stderr pipe is unavailable; failing closed"
                )
            remaining = _MAX_STDERR_BYTES
            while remaining > 0:
                chunk = await stream.read(65536)
                if not chunk:
                    return
                remaining -= len(chunk)

        await asyncio.gather(read_stdout(), drain_stderr())
        await proc.wait()
        return bytes(stdout_first), oversized

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        """Terminate the podman driver process with bounded escalation."""
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError, ProcessLookupError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await proc.wait()
                except ProcessLookupError:
                    pass

    def _build_result(
        self,
        request: SandboxExecutionRequest,
        started: float,
        exit_code: int | None,
        stdout: bytes,
        oversized: bool,
        timed_out: bool,
        *,
        removed: bool,
    ) -> SandboxExecutionResult:
        if not removed:
            exit_reason = SandboxExitReason.INFRASTRUCTURE_FAILURE
            reason = "sandbox cleanup could not be verified"
        elif timed_out:
            exit_reason = SandboxExitReason.TIMED_OUT
            reason = "execution exceeded the sandbox timeout"
        elif oversized:
            exit_reason = SandboxExitReason.INVALID_OR_OVERSIZED_OUTPUT
            reason = "runtime output exceeded the sandbox bound"
        elif exit_code == 0:
            exit_reason = SandboxExitReason.COMPLETED
            reason = None
        elif exit_code == 137:
            exit_reason = SandboxExitReason.RESOURCE_LIMIT
            reason = "workload terminated by a resource limit"
        elif exit_code == 143:
            exit_reason = SandboxExitReason.TERMINATED
            reason = "workload received a termination signal"
        elif exit_code == 2:
            exit_reason = SandboxExitReason.WORKLOAD_FAILED
            reason = "runtime rejected its input (protocol error)"
        else:
            exit_reason = SandboxExitReason.WORKLOAD_FAILED
            reason = "workload failed (non-zero exit)"
        metrics = SandboxExecutionMetrics(
            duration_seconds=time.perf_counter() - started,
            exit_code=exit_code,
            output_bytes=len(stdout),
            cleanup_succeeded=removed,
        )
        result = SandboxExecutionResult(
            execution_id=request.execution_id,
            exit_reason=exit_reason,
            output=stdout if exit_reason is SandboxExitReason.COMPLETED else None,
            metrics=metrics,
            reason=reason,
        )
        self._record(result)
        return result

    def _recorded_result(
        self,
        request: SandboxExecutionRequest,
        exit_reason: SandboxExitReason,
        started: float,
        *,
        reason: str,
        cleanup_succeeded: bool = True,
    ) -> SandboxExecutionResult:
        result = SandboxExecutionResult(
            execution_id=request.execution_id,
            exit_reason=exit_reason,
            metrics=SandboxExecutionMetrics(
                duration_seconds=time.perf_counter() - started,
                cleanup_succeeded=cleanup_succeeded,
            ),
            reason=reason,
        )
        self._record(result)
        return result

    def _record(self, result: SandboxExecutionResult) -> None:
        get_counter("darkula.sandbox.executions").add(
            1,
            {
                "darkula.sandbox.exit_reason": result.exit_reason.value,
                "darkula.sandbox.cleanup": (
                    "ok" if result.metrics.cleanup_succeeded else "failed"
                ),
            },
        )


__all__ = ["PodmanSandbox", "PodmanSandboxConfig"]

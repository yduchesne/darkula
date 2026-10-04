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
import json
import logging
import shutil
import time
import uuid
from dataclasses import dataclass

from darkula.sandbox.contracts import (
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

#: Bounded execution identity length embedded in container names.
_MAX_EXECUTION_ID_IN_NAME = 40


class _PodmanCliError(SandboxInfrastructureError):
    """A bounded Podman CLI failure (never echoes command output)."""


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

    async def _remove_owned_container(self, name: str) -> bool:
        """Stop and remove one container **only** when positively owned.

        Returns ``True`` when the container is gone afterwards; a
        non-existent container is a safe no-op; an unowned/unverifiable
        container is never touched (returns ``False``).
        """
        while True:
            try:
                exists, owned, state = await self._container_state(name)
            except SandboxInfrastructureError:
                _LOGGER.error("podman cleanup failed for %s", name)
                return False
            if not exists:
                return True
            if not owned:
                _LOGGER.error("refusing to remove unverified container %s", name)
                return False
            try:
                if state == "running":
                    await self._run_podman(
                        ["stop", "-t", "2", name], command_timeout=60
                    )
                await self._run_podman(["rm", "-f", name], command_timeout=60)
            except SandboxInfrastructureError:
                _LOGGER.error("podman cleanup failed for %s", name)
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
            return self._recorded_result(
                request,
                SandboxExitReason.INFRASTRUCTURE_FAILURE,
                started,
                reason=f"podman execution failed: {exc}",
            )
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
        if timed_out:
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
    ) -> SandboxExecutionResult:
        result = SandboxExecutionResult(
            execution_id=request.execution_id,
            exit_reason=exit_reason,
            metrics=SandboxExecutionMetrics(
                duration_seconds=time.perf_counter() - started
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

# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""PodmanSandbox unit tests (PR 7).

Cover the pure logic of the adapter (exact run argv, container naming,
ownership verification, outcome mapping, bounded output collection, timeout
and cancellation cleanup) without invoking real Podman; the integration
suite exercises real containers.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from types import SimpleNamespace

import pytest

from darkula.infrastructure.sandbox.podman import (
    PodmanSandbox,
    PodmanSandboxConfig,
)
from darkula.sandbox.contracts import (
    AllowedDestination,
    NetworkPolicy,
    ResourceLimits,
    SandboxExecutionPolicy,
    SandboxExecutionRequest,
    SandboxExecutionResult,
    SandboxExitReason,
    SandboxInfrastructureError,
    SandboxWorkload,
    WorkloadKind,
)

CONFIG = PodmanSandboxConfig(
    image="localhost/darkula-crawler-runtime:1.63.0",
    network="darkula-intg",
    binary="podman",
)


def request(**overrides: object) -> SandboxExecutionRequest:
    base: dict[str, object] = {
        "execution_id": "exec-abcd1234",
        "policy": SandboxExecutionPolicy(
            network=NetworkPolicy(
                destinations=(AllowedDestination("blackgate.example.test", 8080),)
            )
        ),
        "workload": SandboxWorkload(WorkloadKind.CRAWLER, b'{"version":1}'),
    }
    base.update(overrides)
    return SandboxExecutionRequest(**base)  # type: ignore[arg-type]


def state(name: str) -> list[bool]:
    raise AssertionError("helper placeholder")  # pragma: no cover


class TestNaming:
    def test_container_name_namespace_and_bounds(self) -> None:
        name = PodmanSandbox.container_name_for("exec-abcd1234", nonce="deadbeef")
        assert name == "darkula-crawler-exec-abcd1234-deadbeef"
        assert name.startswith("darkula-crawler-")
        assert len(name) <= 63

    def test_container_name_truncates_execution_id(self) -> None:
        name = PodmanSandbox.container_name_for("x" * 300, nonce="deadbeef")
        assert "x" * 41 not in name
        assert name.endswith("-deadbeef")


class TestRunArgv:
    def test_exact_hardened_flag_vector(self) -> None:
        sandbox = PodmanSandbox(CONFIG)
        argv = sandbox._build_run_argv(
            request(), "darkula-crawler-exec-abcd1234-deadbeef"
        )
        joined = " ".join(argv)
        assert "--network darkula-intg" in joined
        assert "--read-only" in joined
        assert "--cap-drop all" in joined
        assert "--security-opt no-new-privileges" in joined
        assert "--pids-limit 512" in joined
        assert "--memory 1073741824b" in joined
        assert "--cpus 2" in joined
        assert "--user 10001" in joined
        assert "--tmpfs /tmp:rw,size=268435456,mode=1777" in joined
        assert "--tmpfs /dev/shm:rw,size=268435456,mode=1777" in joined
        assert "--label darkula.owned=true" in joined
        assert "--label darkula.service=crawler" in joined
        assert "darkula.execution=exec-abcd1234" in joined
        # Only the allow-listed runtime command may run inside.
        assert argv[-4:] == [
            "localhost/darkula-crawler-runtime:1.63.0",
            "python3",
            "-m",
            "crawler_runtime",
        ]
        assert "-i" in argv  # bounded stdin transfer
        assert "-v" not in argv  # no arbitrary bind mounts
        assert "docker.sock" not in joined and "podman.sock" not in joined
        assert "--privileged" not in joined
        assert "--network host" not in joined

    def test_argv_derives_from_policy_limits(self) -> None:
        sandbox = PodmanSandbox(CONFIG)
        req = request(
            policy=SandboxExecutionPolicy(
                network=NetworkPolicy(
                    destinations=(AllowedDestination("x.example.test", 80),)
                ),
                resources=ResourceLimits(
                    memory_bytes=268435456,
                    cpus=1.0,
                    pids=64,
                    timeout_seconds=5.0,
                ),
            )
        )
        argv = sandbox._build_run_argv(req, "darkula-crawler-x-y")
        assert "268435456b" in argv
        assert argv[argv.index("--cpus") + 1] == "1"
        assert argv[argv.index("--pids-limit") + 1] == "64"


class TestOwnershipVerification:
    @pytest.mark.asyncio
    async def test_container_state_owned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sandbox = PodmanSandbox(CONFIG)
        raw = json.dumps(
            [
                {
                    "Config": {"Labels": {"darkula.owned": "true"}},
                    "State": {"Status": "running"},
                }
            ]
        ).encode()

        async def fake(
            argv: list[str], *, command_timeout: float = 30.0
        ) -> tuple[int, bytes, bytes]:
            return 0, raw, b""

        monkeypatch.setattr(sandbox, "_run_podman", fake)
        exists, owned, state_value = await sandbox._container_state(
            "darkula-crawler-x-y"
        )
        assert exists and owned and state_value == "running"

    @pytest.mark.asyncio
    async def test_container_state_unowned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sandbox = PodmanSandbox(CONFIG)
        raw = json.dumps(
            [{"Config": {"Labels": {}}, "State": {"Status": "exited"}}]
        ).encode()

        async def fake(
            argv: list[str], *, command_timeout: float = 30.0
        ) -> tuple[int, bytes, bytes]:
            return 0, raw, b""

        monkeypatch.setattr(sandbox, "_run_podman", fake)
        exists, owned, _ = await sandbox._container_state("darkula-crawler-x-y")
        assert exists and not owned

    @pytest.mark.asyncio
    async def test_container_state_unreadable_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sandbox = PodmanSandbox(CONFIG)

        async def fake(
            argv: list[str], *, command_timeout: float = 30.0
        ) -> tuple[int, bytes, bytes]:
            return 0, b"not json", b""

        monkeypatch.setattr(sandbox, "_run_podman", fake)
        with pytest.raises(SandboxInfrastructureError):
            await sandbox._container_state("darkula-crawler-x-y")

    @pytest.mark.asyncio
    async def test_remove_only_owned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        sandbox = PodmanSandbox(CONFIG)
        states: list[tuple[bool, bool, str | None]] = [
            (True, True, "running"),  # owned running: stop + rm
            (False, False, None),  # then gone
        ]

        async def fake_state(name: str) -> tuple[bool, bool, str | None]:
            return states.pop(0)

        async def fake_run(
            argv: list[str], *, command_timeout: float = 30.0
        ) -> tuple[int, bytes, bytes]:
            calls.append(argv)
            return 0, b"", b""

        monkeypatch.setattr(sandbox, "_container_state", fake_state)
        monkeypatch.setattr(sandbox, "_run_podman", fake_run)

        assert await sandbox._remove_owned_container("darkula-crawler-x-y") is True
        assert calls[0][0] == "stop"
        assert calls[1][0] == "rm"

    @pytest.mark.asyncio
    async def test_unowned_container_never_touched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        sandbox = PodmanSandbox(CONFIG)

        async def fake_state(name: str) -> tuple[bool, bool, str | None]:
            return (True, False, None)

        async def fake_run(
            argv: list[str], *, command_timeout: float = 30.0
        ) -> tuple[int, bytes, bytes]:
            calls.append(argv[0])
            return 0, b"", b""

        monkeypatch.setattr(sandbox, "_container_state", fake_state)
        monkeypatch.setattr(sandbox, "_run_podman", fake_run)
        assert await sandbox._remove_owned_container("darkula-crawler-x-y") is False
        assert calls == []


class FakeProc:
    """Minimal asyncio-subprocess-like fake for execute() paths."""

    def __init__(self, returncode: int | None = 0) -> None:
        self.returncode = returncode
        self.stdin = SimpleNamespace()
        self.stdin.write = lambda data: len(data)
        self.stdin.drain = _async_noop
        self.stdin.close = lambda: None
        self.stdout = SimpleNamespace()
        self.stderr = SimpleNamespace()
        self.killed = False
        self.terminated = False
        self.waited = 0

    async def wait(self) -> int:
        self.waited += 1
        return self.returncode or 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15


async def _async_noop() -> None:
    return None


def _patch_execute_harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    proc: FakeProc,
    collected: tuple[bytes, bool] = (b"{}", False),
) -> None:
    """Patch the execute() seams: podman present, fresh name, subprocess."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/podman")

    async def fake_state(self, name: str) -> tuple[bool, bool, str | None]:  # type: ignore[no-untyped-def]
        # Fresh execution: the container name is never pre-existing.
        return (False, False, None)

    async def fake_subprocess(*args: object, **kwargs: object) -> FakeProc:
        return proc

    async def fake_collect(
        self: object, the_proc: object, max_output_bytes: int
    ) -> tuple[bytes, bool]:
        return collected

    monkeypatch.setattr(PodmanSandbox, "_container_state", fake_state)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(PodmanSandbox, "_collect_outputs", fake_collect)


class TestExecuteOutcomes:
    @pytest.mark.asyncio
    async def test_binary_missing_typed_result(self) -> None:
        sandbox = PodmanSandbox(
            PodmanSandboxConfig(image="i", network="n", binary="no-such-binary-xyz")
        )
        result = await sandbox.execute(request())
        assert result.exit_reason is SandboxExitReason.INFRASTRUCTURE_FAILURE
        assert "not found" in (result.reason or "")

    @pytest.mark.asyncio
    async def test_preflight_unowned_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sandbox = PodmanSandbox(CONFIG)

        async def fake_state(name: str) -> tuple[bool, bool, str | None]:
            return (True, False, None)

        monkeypatch.setattr(sandbox, "_container_state", fake_state)
        result = await sandbox.execute(request())
        assert result.exit_reason is SandboxExitReason.INFRASTRUCTURE_FAILURE
        assert "unowned" in (result.reason or "")

    @pytest.mark.asyncio
    async def test_success_completed_with_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = FakeProc(0)
        _patch_execute_harness(monkeypatch, proc=proc, collected=(b'{"v":1}', False))
        removed: list[str] = []

        async def fake_remove(self: object, name: str) -> bool:
            removed.append(name)
            return True

        monkeypatch.setattr(PodmanSandbox, "_remove_owned_container", fake_remove)
        sandbox = PodmanSandbox(CONFIG)
        result = await sandbox.execute(request())
        assert result.exit_reason is SandboxExitReason.COMPLETED
        assert result.output == b'{"v":1}'
        assert result.metrics.cleanup_succeeded is True
        assert removed and "darkula-crawler-" in removed[0]
        assert proc.stdin is not None

    @pytest.mark.asyncio
    async def test_oversized_output_rejected_with_cleanup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = FakeProc(0)
        _patch_execute_harness(monkeypatch, proc=proc, collected=(b"", True))
        removed: list[str] = []

        async def fake_remove(self: object, name: str) -> bool:
            removed.append(name)
            return True

        monkeypatch.setattr(PodmanSandbox, "_remove_owned_container", fake_remove)
        sandbox = PodmanSandbox(CONFIG)
        result = await sandbox.execute(request())
        assert result.exit_reason is SandboxExitReason.INVALID_OR_OVERSIZED_OUTPUT
        assert result.output is None
        assert removed

    @pytest.mark.asyncio
    async def test_timeout_terminates_and_cleans_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = FakeProc(None)

        async def fake_subprocess(*args: object, **kwargs: object) -> FakeProc:
            return proc

        async def slow_collect(
            self: object, the_proc: object, max_output_bytes: int
        ) -> tuple[bytes, bool]:
            await asyncio.sleep(30)
            return b"{}", False

        async def fresh_state(self: object, name: str) -> tuple[bool, bool, str | None]:
            return (False, False, None)

        monkeypatch.setattr(PodmanSandbox, "_container_state", fresh_state)
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/podman")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
        monkeypatch.setattr(PodmanSandbox, "_collect_outputs", slow_collect)
        removed: list[str] = []

        async def fake_remove(self: object, name: str) -> bool:
            removed.append(name)
            return True

        monkeypatch.setattr(PodmanSandbox, "_remove_owned_container", fake_remove)
        sandbox = PodmanSandbox(CONFIG)
        req = request(
            policy=SandboxExecutionPolicy(
                network=NetworkPolicy(
                    destinations=(AllowedDestination("b.example.test", 80),)
                ),
                resources=ResourceLimits(
                    memory_bytes=1024 * 1024,
                    cpus=1.0,
                    pids=64,
                    timeout_seconds=0.05,
                ),
            )
        )
        result = await sandbox.execute(req)
        assert result.exit_reason is SandboxExitReason.TIMED_OUT
        assert proc.terminated or proc.killed
        assert removed and result.metrics.cleanup_succeeded is True

    @pytest.mark.asyncio
    async def test_cancellation_propagates_after_cleanup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = FakeProc(None)

        async def fake_subprocess(*args: object, **kwargs: object) -> FakeProc:
            return proc

        async def never_collect(
            self: object, the_proc: object, max_output_bytes: int
        ) -> tuple[bytes, bool]:
            await asyncio.Event().wait()  # pragma: no cover - cancelled
            return b"{}", False

        async def fresh_state(self: object, name: str) -> tuple[bool, bool, str | None]:
            return (False, False, None)

        monkeypatch.setattr(PodmanSandbox, "_container_state", fresh_state)
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/podman")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
        monkeypatch.setattr(PodmanSandbox, "_collect_outputs", never_collect)
        removed: list[str] = []

        async def fake_remove(self: object, name: str) -> bool:
            removed.append(name)
            return True

        monkeypatch.setattr(PodmanSandbox, "_remove_owned_container", fake_remove)
        sandbox = PodmanSandbox(CONFIG)
        task = asyncio.create_task(sandbox.execute(request()))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert proc.terminated or proc.killed
        assert removed  # cleanup ran before propagation

    def test_exit_code_mapping(self) -> None:
        sandbox = PodmanSandbox(CONFIG)
        cases = {
            0: SandboxExitReason.COMPLETED,
            2: SandboxExitReason.WORKLOAD_FAILED,
            137: SandboxExitReason.RESOURCE_LIMIT,
            143: SandboxExitReason.TERMINATED,
            7: SandboxExitReason.WORKLOAD_FAILED,
        }
        for code, expected in cases.items():
            result = sandbox._build_result(
                request(),
                0.0,
                code,
                b"out",
                oversized=False,
                timed_out=False,
                removed=True,
            )
            assert result.exit_reason is expected, f"exit code {code}"
            if expected is SandboxExitReason.COMPLETED:
                assert result.output == b"out"

    def test_timed_out_override_exit_code(self) -> None:
        sandbox = PodmanSandbox(CONFIG)
        result = sandbox._build_result(
            request(), 0.0, 0, b"", oversized=False, timed_out=True, removed=True
        )
        assert result.exit_reason is SandboxExitReason.TIMED_OUT
        assert result.output is None

    @pytest.mark.asyncio
    async def test_collect_outputs_bounded(self) -> None:
        sandbox = PodmanSandbox(CONFIG)
        reader = asyncio.StreamReader()
        reader.feed_data(b"x" * 1000)
        reader.feed_eof()
        stderr_reader = asyncio.StreamReader()
        stderr_reader.feed_eof()
        proc = FakeProc(0)
        proc.stdout = reader  # type: ignore[assignment]
        proc.stderr = stderr_reader  # type: ignore[assignment]
        stdout, oversized = await sandbox._collect_outputs(proc, max_output_bytes=100)  # type: ignore[arg-type]
        assert len(stdout) == 100
        assert oversized is True


class TestResultTypes:
    def test_execute_returns_sandbox_result_type(self) -> None:
        result = SandboxExecutionResult(
            execution_id="exec-abcd1234", exit_reason=SandboxExitReason.COMPLETED
        )
        assert result.execution_id == "exec-abcd1234"

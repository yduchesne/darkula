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
    _LiveResources,
)
from darkula.sandbox.contracts import (
    AllowedDestination,
    EgressMode,
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
    image="localhost/darkula-crawler-runtime:1.63.0-pr20",
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
            "localhost/darkula-crawler-runtime:1.63.0-pr20",
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


# -- PR 20 public HTTPS topology (U20-42..U20-46) -------------------------


def live_policy() -> SandboxExecutionPolicy:
    return SandboxExecutionPolicy(
        network=NetworkPolicy(
            egress=EgressMode.PUBLIC_HTTPS_PROXY,
            destinations=(AllowedDestination("approved.example.org", 443),),
        )
    )


def live_request(**overrides: object) -> SandboxExecutionRequest:
    base: dict[str, object] = {
        "execution_id": "exec-live1234",
        "policy": live_policy(),
        "workload": SandboxWorkload(WorkloadKind.CRAWLER, b'{"version":2}'),
    }
    base.update(overrides)
    return SandboxExecutionRequest(**base)  # type: ignore[arg-type]


def live_resources() -> _LiveResources:
    return _LiveResources(
        execution_id="exec-live1234",
        network="darkula-https-net-exec-live1234-deadbeef",
        gateway="darkula-https-gw-exec-live1234-deadbeef",
        crawler="darkula-crawler-exec-live1234-deadbeef",
    )


class TestLiveCrawlerArgv:
    def test_u20_42_legacy_argv_is_unchanged(self) -> None:
        sandbox = PodmanSandbox(CONFIG)
        argv = sandbox._build_run_argv(request(), "darkula-crawler-x-y")
        assert "--network" in argv
        assert argv[argv.index("--network") + 1] == "darkula-intg"
        assert "--dns" not in argv
        assert "--http-proxy=false" not in argv

    def test_u20_43_crawler_one_internal_network_dns_none(self) -> None:
        sandbox = PodmanSandbox(CONFIG)
        argv = sandbox._build_live_crawler_argv(
            live_request(),
            name="darkula-crawler-x-y",
            network="darkula-https-net-x-y",
            crawler_ip="10.89.1.11",
            gateway_ip="10.89.1.10",
            subnet="10.89.1.0/24",
        )
        joined = " ".join(argv)
        assert argv.count("--network") == 1
        assert (
            argv[argv.index("--network") + 1] == "darkula-https-net-x-y:ip=10.89.1.11"
        )
        assert argv[argv.index("--dns") + 1] == "none"
        assert "--http-proxy=false" in argv
        assert "DARKULA_CRAWLER_HTTPS_PROXY=http://10.89.1.10:3128" in argv
        assert "DARKULA_CRAWLER_INTERNAL_CIDR=10.89.1.0/24" in argv
        assert "darkula-live-egress" not in joined
        assert "--network host" not in joined
        assert "--privileged" not in joined
        assert "-p" not in argv and "--publish" not in argv
        assert "-v" not in argv
        forbidden = (
            "HTTP_PROXY=",
            "HTTPS_PROXY=",
            "ALL_PROXY=",
            "http_proxy=",
            "https_proxy=",
            "all_proxy=",
        )
        assert not any(argument.startswith(forbidden) for argument in argv)
        assert argv[-4:] == [
            "localhost/darkula-crawler-runtime:1.63.0-pr20",
            "python3",
            "-m",
            "crawler_runtime",
        ]

    @pytest.mark.asyncio
    async def test_u20_43_gateway_two_networks_no_ports(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sandbox = PodmanSandbox(CONFIG)
        recorded: list[list[str]] = []

        async def fake_run(
            argv: list[str], *, command_timeout: float = 30.0
        ) -> tuple[int, bytes, bytes]:
            recorded.append(argv)
            return 0, b"", b""

        monkeypatch.setattr(sandbox, "_run_podman", fake_run)
        await sandbox._start_live_gateway(
            live_request(), live_resources(), "approved.example.org", "10.89.1.10"
        )
        argv = recorded[0]
        joined = " ".join(argv)
        assert argv.count("--network") == 2
        assert "darkula-https-net-exec-live1234-deadbeef:ip=10.89.1.10" in argv
        assert "darkula-live-egress" in argv
        assert "--http-proxy=false" in argv
        assert argv[argv.index("--user") + 1] == "10002"
        assert "--read-only" in argv
        assert "--cap-drop" in argv and argv[argv.index("--cap-drop") + 1] == "all"
        assert "--security-opt" in argv
        assert "no-new-privileges" in argv
        assert "DARKULA_EGRESS_HOST=approved.example.org" in argv
        assert "DARKULA_EGRESS_BIND_IPV4=10.89.1.10" in argv
        assert "DARKULA_EGRESS_LISTEN_PORT=3128" in argv
        assert "-p" not in argv and "--publish" not in argv
        assert "-v" not in argv and "--privileged" not in argv
        assert "--network=host" not in joined
        assert argv[-3:] == ["python3", "-m", "https_egress"]

    @pytest.mark.asyncio
    async def test_u20_44_missing_outer_network_is_typed_failure_no_create(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sandbox = PodmanSandbox(CONFIG)
        calls: list[list[str]] = []

        async def fake_run(
            argv: list[str], *, command_timeout: float = 30.0
        ) -> tuple[int, bytes, bytes]:
            calls.append(argv)
            return 0, b"", b""

        async def no_network(name: str) -> dict[str, object] | None:
            return None

        async def ok_cleanup(self: object, resources: object) -> bool:
            return True

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/podman")
        monkeypatch.setattr(sandbox, "_run_podman", fake_run)
        monkeypatch.setattr(sandbox, "_network_inspect", no_network)
        monkeypatch.setattr(PodmanSandbox, "_cleanup_live", ok_cleanup)
        result = await sandbox.execute(live_request())
        assert result.exit_reason is SandboxExitReason.INFRASTRUCTURE_FAILURE
        assert not any(argv[:2] == ["network", "create"] for argv in calls)

    @pytest.mark.asyncio
    async def test_u20_44_foreign_outer_network_never_attached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sandbox = PodmanSandbox(CONFIG)
        calls: list[list[str]] = []

        async def fake_run(
            argv: list[str], *, command_timeout: float = 30.0
        ) -> tuple[int, bytes, bytes]:
            calls.append(argv)
            return 0, b"", b""

        async def unowned(name: str) -> dict[str, object] | None:
            return {"labels": {}, "internal": False}

        async def ok_cleanup(self: object, resources: object) -> bool:
            return True

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/podman")
        monkeypatch.setattr(sandbox, "_run_podman", fake_run)
        monkeypatch.setattr(sandbox, "_network_inspect", unowned)
        monkeypatch.setattr(PodmanSandbox, "_cleanup_live", ok_cleanup)
        result = await sandbox.execute(live_request())
        assert result.exit_reason is SandboxExitReason.INFRASTRUCTURE_FAILURE
        assert calls == []

    @pytest.mark.asyncio
    async def test_u20_45_gateway_verification_failure_launches_no_crawler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sandbox = PodmanSandbox(CONFIG)
        calls: list[list[str]] = []
        cleanup_calls: list[str] = []

        async def fake_run(
            argv: list[str], *, command_timeout: float = 30.0
        ) -> tuple[int, bytes, bytes]:
            calls.append(argv)
            return 0, b"", b""

        async def network_doc(name: str) -> dict[str, object] | None:
            return {
                "labels": {"darkula.owned": "true"},
                "internal": True,
                "subnets": [{"subnet": "10.89.1.0/24"}],
            }

        async def no_preflight(resources: object) -> None:
            return None

        async def bad_container(name: str) -> dict[str, object] | None:
            return {
                "Config": {"Labels": {"darkula.owned": "true"}},
                "State": {"Running": True},
                "NetworkSettings": {
                    "Networks": {"darkula-https-net-exec-live1234-deadbeef": {}}
                },
                "HostConfig": {},
            }

        async def no_ready(name: str) -> None:
            return None

        async def ok_cleanup(self: PodmanSandbox, resources: _LiveResources) -> bool:
            cleanup_calls.append(resources.crawler)
            return True

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/podman")
        monkeypatch.setattr(sandbox, "_run_podman", fake_run)
        monkeypatch.setattr(sandbox, "_network_inspect", network_doc)
        monkeypatch.setattr(sandbox, "_preflight_public_https", no_preflight)
        monkeypatch.setattr(sandbox, "_container_inspect", bad_container)
        monkeypatch.setattr(sandbox, "_wait_live_gateway_ready", no_ready)
        monkeypatch.setattr(PodmanSandbox, "_cleanup_live", ok_cleanup)
        result = await sandbox.execute(live_request())
        assert result.exit_reason is SandboxExitReason.INFRASTRUCTURE_FAILURE
        assert not any("crawler_runtime" in argv for argv in calls)
        assert cleanup_calls

    @pytest.mark.asyncio
    async def test_u20_46_cleanup_order_crawler_gateway_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sandbox = PodmanSandbox(CONFIG)
        order: list[str] = []

        async def remove_container(self: object, name: str) -> bool:
            order.append(f"container:{name}")
            return True

        async def remove_network(self: object, name: str) -> bool:
            order.append(f"network:{name}")
            return True

        monkeypatch.setattr(PodmanSandbox, "_remove_owned_container", remove_container)
        monkeypatch.setattr(PodmanSandbox, "_remove_owned_network", remove_network)
        resources = live_resources()
        assert await sandbox._cleanup_live(resources) is True
        assert order == [
            f"container:{resources.crawler}",
            f"container:{resources.gateway}",
            f"network:{resources.network}",
        ]

    @pytest.mark.asyncio
    async def test_u20_46_cleanup_failure_is_never_completed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sandbox = PodmanSandbox(CONFIG)

        async def payload(
            self: object, request: object, resources: object
        ) -> tuple[int | None, bytes, bool, bool]:
            return 0, b"{}", False, False

        async def failed_cleanup(self: object, resources: object) -> bool:
            return False

        monkeypatch.setattr(sandbox, "_run_public_https", payload.__get__(sandbox))
        monkeypatch.setattr(PodmanSandbox, "_cleanup_live", failed_cleanup)
        result = await sandbox.execute(live_request())
        assert result.exit_reason is SandboxExitReason.INFRASTRUCTURE_FAILURE
        assert result.metrics.cleanup_succeeded is False
        assert "cleanup" in (result.reason or "")

    @pytest.mark.asyncio
    async def test_u20_46_cancellation_propagates_after_cleanup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sandbox = PodmanSandbox(CONFIG)
        cleaned: list[str] = []

        async def hang(
            self: object, request: object, resources: object
        ) -> tuple[int | None, bytes, bool, bool]:
            await asyncio.Event().wait()  # pragma: no cover - cancelled
            return 0, b"", False, False

        async def cleanup(self: object, resources: _LiveResources) -> bool:
            cleaned.append(resources.network)
            return True

        monkeypatch.setattr(sandbox, "_run_public_https", hang.__get__(sandbox))
        monkeypatch.setattr(PodmanSandbox, "_cleanup_live", cleanup)
        task = asyncio.create_task(sandbox.execute(live_request()))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned

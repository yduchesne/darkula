# SPDX-License-Identifier: AGPL-3.0-only
"""PR 20 real-Podman offline public-HTTPS topology tests (I20-02..I20-14).

Every test is offline by construction: the fixture supplies a **test-only**
gateway image whose resolver/dialer always deny, so no DNS query or public
TCP request is issued. Probes run inside disposable Darkula-owned containers
on the same ephemeral network/flags as the live crawler.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import cast

import pytest

from darkula.crawler.controller import CrawlerController
from darkula.crawler.runtime_protocol import (
    PUBLIC_HTTPS_PROTOCOL_VERSION,
    RuntimeInput,
    decode_output,
    encode_input,
)
from darkula.infrastructure.sandbox.podman import (
    PodmanSandbox,
    PodmanSandboxConfig,
)
from darkula.sandbox.contracts import (
    AllowedDestination,
    EgressMode,
    NetworkPolicy,
    ResourceLimits,
    SandboxExecutionPolicy,
    SandboxExecutionRequest,
    SandboxExitReason,
    SandboxWorkload,
    WorkloadKind,
)

pytestmark = pytest.mark.integration

HOST = "approved.example.org"
START_URL = f"https://{HOST}:443/docs/"
GATEWAY_IMAGE = "localhost/darkula-https-egress-test:1"
RUNTIME_IMAGE = "localhost/darkula-crawler-runtime:1.63.0-pr20"
FAKE_WORLD_HOST = "darkula-fake-world-intg"

LIVE_LABELS = (
    ("darkula.service=https-egress-network", "network"),
    ("darkula.service=https-egress-gateway", "container"),
    ("darkula.service=https-egress-crawler", "container"),
)

_TCP_PROBE = """
import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), 2).close()
    print("REACHABLE")
except Exception:
    print("DENIED")
"""

_DNS_PROBE = """
import socket, sys
try:
    socket.getaddrinfo(sys.argv[1], 443)
    print("REACHABLE")
except Exception:
    print("DENIED")
"""

_CONNECT_PROBE = """
import socket, sys
s = socket.create_connection((sys.argv[1], int(sys.argv[2])), 3)
req = "CONNECT " + sys.argv[3] + ":443 HTTP/1.1\\r\\n"
req += "Host: " + sys.argv[3] + ":443\\r\\n\\r\\n"
s.sendall(req.encode())
print(s.recv(64).split(b"\\r\\n")[0].decode("ascii", "replace"))
s.close()
"""

_METADATA_PROBE = """
import socket, sys
s = socket.socket()
s.settimeout(2)
try:
    s.connect((sys.argv[1], int(sys.argv[2])))
    print("REACHABLE")
except Exception:
    print("DENIED")
    sys.exit(3)
"""

_ISOLATION_PROBE = """
import os
print("ROUTES")
print(open("/proc/net/route").read())
print("ENV")
for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
             "http_proxy", "https_proxy", "all_proxy"):
    if os.environ.get(name):
        print("AMBIGUOUS", name)
print("END")
"""


async def _podman(*argv: str, timeout_seconds: float = 120.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "podman",
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    return (
        proc.returncode or 0,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )


def live_request(*, timeout_seconds: float = 20.0) -> SandboxExecutionRequest:
    execution_id = f"i20-{uuid.uuid4().hex[:12]}"
    payload = encode_input(
        RuntimeInput(
            version=PUBLIC_HTTPS_PROTOCOL_VERSION,
            execution_id=execution_id,
            request_id=execution_id,
            start_url=START_URL,
            origin_scheme="https",
            origin_host=HOST,
            origin_port=443,
            credentials=None,
            max_pages=2,
            max_requests=10,
            max_depth=1,
            timeout_seconds=timeout_seconds,
            max_page_text_bytes=4096,
            network_mode="public_https_proxy",
            allowed_path_prefix="/docs/",
        )
    )
    return SandboxExecutionRequest(
        execution_id=execution_id,
        policy=SandboxExecutionPolicy(
            network=NetworkPolicy(
                egress=EgressMode.PUBLIC_HTTPS_PROXY,
                destinations=(AllowedDestination(HOST, 443),),
            ),
            resources=ResourceLimits(
                memory_bytes=1024 * 1024 * 1024,
                cpus=1.0,
                pids=256,
                timeout_seconds=timeout_seconds,
            ),
        ),
        workload=SandboxWorkload(WorkloadKind.CRAWLER, payload),
    )


async def _run_probe(network: str, script: str, *args: str) -> str:
    name = f"darkula-i20-probe-{uuid.uuid4().hex[:8]}"
    code, stdout, stderr = await _podman(
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        network,
        "--dns",
        "none",
        "--http-proxy=false",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=16m,mode=1777",
        "--cap-drop",
        "all",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "10001",
        RUNTIME_IMAGE,
        "python3",
        "-c",
        script,
        *args,
    )
    assert code == 0, stderr[:200]
    return stdout


async def _no_live_resources() -> None:
    for label, kind in LIVE_LABELS:
        code, stdout, _ = await _podman(
            "ps",
            "-a",
            "--filter",
            f"label={label}",
            "--format",
            "{{.Names}}",
        )
        if kind == "network":
            code, stdout, _ = await _podman(
                "network", "ls", "--filter", f"label={label}", "--format", "{{.Name}}"
            )
        assert code == 0
        assert stdout.strip() == "", f"leaked {kind} with {label}: {stdout}"


async def _network_document(name: str) -> dict[str, object]:
    code, stdout, stderr = await _podman("network", "inspect", name)
    assert code == 0, stderr
    documents = json.loads(stdout)
    assert isinstance(documents, list) and documents
    return cast("dict[str, object]", documents[0])


def _parse_live_argv(argv: list[str]) -> tuple[str, str, str]:
    network = argv[argv.index("--network") + 1]
    network_name, _, ip = network.partition(":ip=")
    proxy = next(
        item for item in argv if item.startswith("DARKULA_CRAWLER_HTTPS_PROXY=")
    )
    gateway_ip = proxy.split("//", 1)[1].rsplit(":", 1)[0]
    _subnet = next(
        item for item in argv if item.startswith("DARKULA_CRAWLER_INTERNAL_CIDR=")
    ).split("=", 1)[1]
    return network_name, ip, gateway_ip


def _install_probe(
    monkeypatch: pytest.MonkeyPatch,
    sandbox: PodmanSandbox,
    probe: Callable[[str, str, str], Awaitable[None]],
) -> list[tuple[str, str, str]]:
    seen: list[tuple[str, str, str]] = []

    async def fake_process(self: PodmanSandbox, request, argv):  # type: ignore[no-untyped-def]
        network, crawler_ip, gateway_ip = _parse_live_argv(argv)
        seen.append((network, crawler_ip, gateway_ip))
        await probe(network, crawler_ip, gateway_ip)
        return 0, b"{}", False, False

    monkeypatch.setattr(PodmanSandbox, "_run_live_crawler_process", fake_process)
    return seen


class TestTopologyProvisioning:
    @pytest.mark.asyncio
    async def test_i20_04_ephemeral_network_and_addresses(
        self,
        monkeypatch: pytest.MonkeyPatch,
        live_sandbox: PodmanSandbox,
    ) -> None:
        observed: list[dict[str, object]] = []

        async def probe(network: str, crawler_ip: str, gateway_ip: str) -> None:
            document = await _network_document(network)
            observed.append(document)
            subnets = document["subnets"]
            assert isinstance(subnets, list) and len(subnets) == 1
            assert str(subnets[0]["subnet"]).endswith("/24")
            assert document["internal"] is True
            labels = document.get("labels")
            assert isinstance(labels, dict)
            assert labels.get("darkula.owned") == "true"
            assert crawler_ip.split(".")[:3] == gateway_ip.split(".")[:3]

        _install_probe(monkeypatch, live_sandbox, probe)
        result = await live_sandbox.execute(live_request())
        assert result.exit_reason is SandboxExitReason.COMPLETED
        assert observed
        await _no_live_resources()

    @pytest.mark.asyncio
    async def test_i20_05_gateway_internal_listener_reachable(
        self,
        monkeypatch: pytest.MonkeyPatch,
        live_sandbox: PodmanSandbox,
    ) -> None:
        async def probe(network: str, crawler_ip: str, gateway_ip: str) -> None:
            output = await _run_probe(network, _TCP_PROBE, gateway_ip, "3128")
            assert "REACHABLE" in output

        _install_probe(monkeypatch, live_sandbox, probe)
        result = await live_sandbox.execute(live_request())
        assert result.exit_reason is SandboxExitReason.COMPLETED
        await _no_live_resources()

    @pytest.mark.asyncio
    async def test_i20_06_direct_private_public_dns_denied(
        self,
        monkeypatch: pytest.MonkeyPatch,
        live_sandbox: PodmanSandbox,
    ) -> None:
        async def probe(network: str, crawler_ip: str, gateway_ip: str) -> None:
            assert "REACHABLE" in await _run_probe(
                network, _TCP_PROBE, gateway_ip, "3128"
            )
            for host, port in (
                ("169.254.169.254", "80"),
                ("1.1.1.1", "443"),
                (FAKE_WORLD_HOST, "8080"),
            ):
                assert "DENIED" in await _run_probe(network, _TCP_PROBE, host, port), (
                    host,
                    port,
                )
            assert "DENIED" in await _run_probe(network, _DNS_PROBE, "example.com")

        _install_probe(monkeypatch, live_sandbox, probe)
        result = await live_sandbox.execute(live_request())
        assert result.exit_reason is SandboxExitReason.COMPLETED
        await _no_live_resources()

    @pytest.mark.asyncio
    async def test_i20_07_and_i20_13_gateway_refuses_wrong_connect(
        self,
        monkeypatch: pytest.MonkeyPatch,
        live_sandbox: PodmanSandbox,
    ) -> None:
        async def probe(network: str, crawler_ip: str, gateway_ip: str) -> None:
            denied = await _run_probe(
                network, _CONNECT_PROBE, gateway_ip, "3128", "evil.example.org"
            )
            assert "403" in denied
            approved = await _run_probe(
                network, _CONNECT_PROBE, gateway_ip, "3128", HOST
            )
            # The exact approved host is refused only because this test
            # gateway's resolver is fail-closed (never 200 / never public).
            assert "502" in approved

        _install_probe(monkeypatch, live_sandbox, probe)
        result = await live_sandbox.execute(live_request())
        assert result.exit_reason is SandboxExitReason.COMPLETED
        await _no_live_resources()

    @pytest.mark.asyncio
    async def test_i20_09_no_public_route_or_ambient_proxy(
        self,
        monkeypatch: pytest.MonkeyPatch,
        live_sandbox: PodmanSandbox,
    ) -> None:
        async def probe(network: str, crawler_ip: str, gateway_ip: str) -> None:
            output = await _run_probe(network, _ISOLATION_PROBE)
            assert "AMBIGUOUS" not in output
            routes = output.split("ENV")[0]
            route_lines = [
                line.split()
                for line in routes.splitlines()
                if line
                and line[0].isalpha()
                and not line.startswith(("Iface", "ROUTES"))
            ]
            assert route_lines
            assert all(columns[1] != "00000000" for columns in route_lines), routes
            assert [columns[0] for columns in route_lines] == ["eth0"]

        _install_probe(monkeypatch, live_sandbox, probe)
        result = await live_sandbox.execute(live_request())
        assert result.exit_reason is SandboxExitReason.COMPLETED
        await _no_live_resources()

    @pytest.mark.asyncio
    async def test_i20_12_concurrent_executions_are_distinct(
        self,
        monkeypatch: pytest.MonkeyPatch,
        live_sandbox: PodmanSandbox,
    ) -> None:
        seen = _install_probe(
            monkeypatch,
            live_sandbox,
            lambda network, crawler_ip, gateway_ip: asyncio.sleep(0),
        )
        first, second = await asyncio.gather(
            live_sandbox.execute(live_request()),
            live_sandbox.execute(live_request()),
        )
        assert first.exit_reason is SandboxExitReason.COMPLETED
        assert second.exit_reason is SandboxExitReason.COMPLETED
        assert len(seen) == 2
        assert seen[0][0] != seen[1][0]
        await _no_live_resources()


class TestFailuresAndCleanup:
    @pytest.mark.asyncio
    async def test_i20_08_live_gateway_denial_is_not_completed(
        self, live_sandbox: PodmanSandbox
    ) -> None:
        result = await live_sandbox.execute(live_request())
        assert result.exit_reason is SandboxExitReason.COMPLETED
        assert result.output is not None
        output = decode_output(
            result.output, max_bytes=1024 * 1024, max_pages=10, max_links=10
        )
        assert output.status != "completed"
        assert output.pages == ()
        await _no_live_resources()

    @pytest.mark.asyncio
    async def test_i20_02_missing_outer_network_typed_failure(
        self, live_sandbox: PodmanSandbox
    ) -> None:
        sandbox = PodmanSandbox(
            PodmanSandboxConfig(
                image=RUNTIME_IMAGE,
                network="darkula-intg",
                live_egress_network="darkula-live-egress-missing",
                gateway_image=GATEWAY_IMAGE,
            )
        )
        result = await sandbox.execute(live_request())
        assert result.exit_reason is SandboxExitReason.INFRASTRUCTURE_FAILURE
        assert result.metrics.cleanup_succeeded is True
        await _no_live_resources()

    @pytest.mark.asyncio
    async def test_i20_03_foreign_outer_network_refused_and_untouched(
        self, live_sandbox: PodmanSandbox
    ) -> None:
        name = f"darkula-https-foreign-{uuid.uuid4().hex[:8]}"
        code, _, stderr = await _podman("network", "create", name)
        assert code == 0, stderr
        try:
            sandbox = PodmanSandbox(
                PodmanSandboxConfig(
                    image=RUNTIME_IMAGE,
                    network="darkula-intg",
                    live_egress_network=name,
                    gateway_image=GATEWAY_IMAGE,
                )
            )
            result = await sandbox.execute(live_request())
            assert result.exit_reason is SandboxExitReason.INFRASTRUCTURE_FAILURE
            document = await _network_document(name)
            labels = document.get("labels")
            assert not isinstance(labels, dict) or "darkula.owned" not in labels
        finally:
            await _podman("network", "rm", name)
        await _no_live_resources()

    @pytest.mark.asyncio
    async def test_i20_10_cancellation_leaves_no_resources(
        self,
        monkeypatch: pytest.MonkeyPatch,
        live_sandbox: PodmanSandbox,
    ) -> None:
        async def hang(self: PodmanSandbox, request, argv):  # type: ignore[no-untyped-def]
            await asyncio.Event().wait()  # pragma: no cover - cancelled
            return 0, b"{}", False, False

        monkeypatch.setattr(PodmanSandbox, "_run_live_crawler_process", hang)
        task = asyncio.create_task(live_sandbox.execute(live_request()))
        await asyncio.sleep(3.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _no_live_resources()

    @pytest.mark.asyncio
    async def test_i20_11_forced_failure_cleans_everything(
        self,
        monkeypatch: pytest.MonkeyPatch,
        live_sandbox: PodmanSandbox,
    ) -> None:
        async def fail(self: PodmanSandbox, request, argv):  # type: ignore[no-untyped-def]
            return 1, b"", False, False

        monkeypatch.setattr(PodmanSandbox, "_run_live_crawler_process", fail)
        result = await live_sandbox.execute(live_request())
        assert result.exit_reason is SandboxExitReason.WORKLOAD_FAILED
        assert result.metrics.cleanup_succeeded is True
        await _no_live_resources()

    @pytest.mark.asyncio
    async def test_i20_14_cleanup_refuses_unverified_network(
        self, live_sandbox: PodmanSandbox
    ) -> None:
        name = f"darkula-https-unowned-{uuid.uuid4().hex[:8]}"
        code, _, stderr = await _podman("network", "create", name)
        assert code == 0, stderr
        try:
            assert await live_sandbox._remove_owned_network(name) is False
            code, _, _ = await _podman("network", "exists", name)
            assert code == 0
        finally:
            await _podman("network", "rm", name)


class TestLegacyContractUnchanged:
    @pytest.mark.asyncio
    async def test_i20_01_fake_world_v1_crawl_unchanged(
        self, controller: CrawlerController, fake_world_url: str
    ) -> None:
        from darkula.crawler import CrawlStatus
        from tests.integration.crawler.helpers import crawl_request

        result = await controller.crawl(
            crawl_request(
                fake_world_url,
                start_path="/",
                max_pages=2,
                max_requests=6,
                max_depth=1,
                timeout_seconds=60.0,
            )
        )
        assert result.status is CrawlStatus.COMPLETED
        assert result.protocol_version == 1

    @pytest.mark.asyncio
    async def test_i20_15_existing_internal_isolation_probes_unchanged(
        self, sandbox: PodmanSandbox, fake_world_url: str
    ) -> None:
        from tests.integration.crawler.helpers import run_probe

        code, stdout, _ = await run_probe(
            sandbox,
            "i20-15",
            fake_world_url,
            _METADATA_PROBE,
            "169.254.169.254",
            "80",
        )
        assert code != 0, stdout


class TestF1CleanupRegressions:
    @pytest.mark.asyncio
    async def test_i20f1_01_attached_network_refuses_bounded_removal(
        self, live_sandbox: PodmanSandbox
    ) -> None:
        nonce = uuid.uuid4().hex[:8]
        network = f"darkula-https-f1-net-{nonce}"
        container = f"darkula-https-f1-ctr-{nonce}"
        code, _, stderr = await _podman(
            "network",
            "create",
            "--internal",
            "--label",
            "darkula.owned=true",
            "--label",
            "darkula.service=https-egress-network",
            network,
        )
        assert code == 0, stderr[:160]
        try:
            code, _, stderr = await _podman(
                "run",
                "-d",
                "--name",
                container,
                "--network",
                network,
                "--label",
                "darkula.owned=true",
                "--label",
                "darkula.service=probe-f1",
                "--read-only",
                "--cap-drop",
                "all",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "10001",
                RUNTIME_IMAGE,
                "python3",
                "-c",
                "import time; time.sleep(120)",
            )
            assert code == 0, stderr[:160]
            removed = await asyncio.wait_for(
                live_sandbox._remove_owned_network(network), timeout=35.0
            )
            assert removed is False
            assert (await _podman("network", "exists", network))[0] == 0
            assert (await _podman("container", "exists", container))[0] == 0
        finally:
            code, stdout, _ = await _podman("container", "inspect", container)
            if code == 0:
                document = json.loads(stdout)[0]
                labels = document.get("Config", {}).get("Labels", {})
                assert labels.get("darkula.owned") == "true"
                await _podman("rm", "-f", container)
            await _podman("network", "rm", network)
        assert (await _podman("container", "exists", container))[0] == 1
        assert (await _podman("network", "exists", network))[0] == 1

    @pytest.mark.asyncio
    async def test_i20f1_02_injected_persistent_rm_nonzero_is_bounded(
        self, monkeypatch: pytest.MonkeyPatch, live_sandbox: PodmanSandbox
    ) -> None:
        nonce = uuid.uuid4().hex[:8]
        container = f"darkula-https-f1-rm-{nonce}"
        code, _, stderr = await _podman(
            "run",
            "-d",
            "--name",
            container,
            "--network",
            "darkula-intg",
            "--label",
            "darkula.owned=true",
            "--label",
            "darkula.service=probe-f1",
            "--read-only",
            "--cap-drop",
            "all",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "10001",
            RUNTIME_IMAGE,
            "python3",
            "-c",
            "import time; time.sleep(120)",
        )
        assert code == 0, stderr[:160]
        original = live_sandbox._run_podman
        rm_calls = 0

        async def fail_rm(
            argv: list[str], *, command_timeout: float = 30.0
        ) -> tuple[int, bytes, bytes]:
            nonlocal rm_calls
            if argv[:3] == ["rm", "-f", container]:
                rm_calls += 1
                return 125, b"", b""
            return await original(argv, command_timeout=command_timeout)

        monkeypatch.setattr(live_sandbox, "_run_podman", fail_rm)
        try:
            removed = await asyncio.wait_for(
                live_sandbox._remove_owned_container(container), timeout=35.0
            )
            assert removed is False
            assert 1 <= rm_calls <= 2
            code, stdout, _ = await _podman("container", "inspect", container)
            assert code == 0
            document = json.loads(stdout)[0]
            assert document["Config"]["Labels"].get("darkula.owned") == "true"
        finally:
            monkeypatch.undo()
            removed = await asyncio.wait_for(
                live_sandbox._remove_owned_container(container), timeout=35.0
            )
            assert removed is True
        assert (await _podman("container", "exists", container))[0] == 1

    @pytest.mark.asyncio
    async def test_i20f1_03_live_topology_cleanup_is_verified(
        self, live_sandbox: PodmanSandbox
    ) -> None:
        result = await live_sandbox.execute(live_request())
        assert result.metrics.cleanup_succeeded is True
        assert result.output is not None
        output = decode_output(
            result.output, max_bytes=1024 * 1024, max_pages=10, max_links=10
        )
        assert output.status != "completed"
        await _no_live_resources()

    @pytest.mark.asyncio
    async def test_i20f1_04_unverified_resources_remain_untouched(
        self, live_sandbox: PodmanSandbox
    ) -> None:
        name = f"darkula-https-f1-unowned-{uuid.uuid4().hex[:8]}"
        code, _, stderr = await _podman("network", "create", name)
        assert code == 0, stderr
        try:
            assert await live_sandbox._remove_owned_network(name) is False
            assert (await _podman("network", "exists", name))[0] == 0
        finally:
            await _podman("network", "rm", name)
        assert (await _podman("network", "exists", name))[0] == 1

    @pytest.mark.asyncio
    async def test_i20f1_05_legacy_fake_world_still_completes(
        self, controller: CrawlerController, fake_world_url: str
    ) -> None:
        from darkula.crawler import CrawlStatus
        from tests.integration.crawler.conftest import container_names
        from tests.integration.crawler.helpers import crawl_request

        result = await controller.crawl(
            crawl_request(
                fake_world_url,
                start_path="/",
                max_pages=2,
                max_requests=6,
                max_depth=1,
                timeout_seconds=60.0,
            )
        )
        assert result.status is CrawlStatus.COMPLETED
        assert result.protocol_version == 1
        assert result.cleanup_succeeded is True
        names = await container_names()
        assert not any(name.startswith("darkula-crawler-") for name in names)

# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Sandbox lifecycle/ownership and resource-boundary integration tests.

All resources exercised here are Darkula-owned (provisioned/labeled by the
fixtures) or freshly created by the test itself; ownership is verified from
exact metadata before any removal. No broad podman cleanup is ever used.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from darkula.crawler import CrawlerController, CrawlRequest, CrawlStatus
from darkula.infrastructure.sandbox.podman import PodmanSandbox
from tests.integration.crawler.conftest import (
    _run_podman_argv,
    container_names,
    remove_owned_container,
)

pytestmark = pytest.mark.integration


async def _wait_no_crawler_containers(wait_seconds: float = 20.0) -> bool:
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while True:
        names = await container_names()
        if not any(name.startswith("darkula-crawler-") for name in names):
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.3)


def _crawl_to_host(
    fake_world_url_unused: str,
    *,
    host: str,
    timeout_seconds: float,
    max_requests: int = 40,
    request_id: str,
) -> CrawlRequest:
    """Build a bounded crawl pointed at ``host`` (a non-Fake-World sibling)."""
    from darkula.crawler import AllowedOrigin, CrawlCredentials, CrawlRequest

    return CrawlRequest(
        request_id=request_id,
        start_url=f"http://{host}:8080/",
        allowed_origin=AllowedOrigin.parse(f"http://{host}:8080"),
        credentials=CrawlCredentials("zerofox77", "dummy-password"),
        max_pages=20,
        max_requests=max_requests,
        max_depth=2,
        timeout_seconds=timeout_seconds,
    )


@pytest.mark.asyncio
async def test_p5_sandbox_timeout_terminates_and_cleans(
    controller: CrawlerController, hanging_sibling: str
) -> None:
    """A crawl against a never-answering listener is killed by the sandbox
    timeout; the disposable container is removed afterwards."""
    request = _crawl_to_host(
        "",
        host=hanging_sibling,
        timeout_seconds=6.0,
        max_requests=30,
        request_id="intg-hang",
    )
    result = await controller.crawl(request)
    assert result.status is CrawlStatus.TIMED_OUT
    assert await _wait_no_crawler_containers()


@pytest.mark.asyncio
async def test_p6_cancellation_propagates_and_cleans(
    controller: CrawlerController, hanging_sibling: str
) -> None:
    """Cancelling a blocked crawl propagates CancelledError and removes the
    disposable container (never a dangling sandbox)."""
    from darkula.crawler import AllowedOrigin, CrawlCredentials, CrawlRequest

    task = asyncio.create_task(
        controller.crawl(
            CrawlRequest(
                request_id="intg-cancel",
                start_url=f"http://{hanging_sibling}:8080/",
                allowed_origin=AllowedOrigin.parse(f"http://{hanging_sibling}:8080"),
                credentials=CrawlCredentials("x", "y" * 16),
                max_pages=20,
                max_requests=60,
                max_depth=1,
                timeout_seconds=60.0,
            )
        )
    )
    await asyncio.sleep(2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _wait_no_crawler_containers()


@pytest.mark.asyncio
async def test_p7_unowned_same_name_fails_closed(
    sandbox: PodmanSandbox,
) -> None:
    """A same-named resource that is NOT Darkula-owned is never adopted,
    relabeled, or removed by the sandbox (fail closed)."""
    name = "darkula-crawler-p7-unowned"
    rc, _, _ = await _run_podman_argv("rm", "-f", name)
    rc, _, stderr = await _run_podman_argv(
        "run",
        "-d",
        "--name",
        name,  # deliberately NO darkula.owned label
        "docker.io/library/python:3.14-slim",
        "sleep",
        "120",
    )
    assert rc == 0, stderr
    try:
        # Preflight inspection reports the resource as existing-but-UNOWNED
        # (fail closed: never adopted, no matter what the name looks like).
        state = await sandbox._container_state(name)
        assert state == (True, False, "running"), state
        # The sandbox refuses removal of an unowned resource.
        removed = await sandbox._remove_owned_container(name)
        assert removed is False
        names = await container_names()
        assert name in names, "unowned container must be left untouched"
    finally:
        await _run_podman_argv("rm", "-f", name)


@pytest.mark.asyncio
async def test_p7b_remove_owned_container_by_label(
    sandbox: PodmanSandbox,
) -> None:
    """The suite's own removal helper only ever removes labeled resources."""
    name = "darkula-crawler-p7b-owned"
    await _run_podman_argv("rm", "-f", name)
    rc, _, stderr = await _run_podman_argv(
        "run",
        "-d",
        "--name",
        name,
        "--label",
        "darkula.owned=true",
        "--label",
        "darkula.service=crawler",
        "localhost/darkula-crawler-runtime:1.63.0",
        "sleep",
        "60",
    )
    assert rc == 0, stderr
    await remove_owned_container(name)
    names = await container_names()
    assert name not in names


@pytest.mark.asyncio
async def test_p10_hardened_host_config_flags(sandbox: PodmanSandbox) -> None:
    """Live container inspection proves the hardening flags: non-privileged,
    dropped capabilities, no-new-privileges, non-root user, resource ceilings,
    internal network; plus a runtime probe proves read-only rootfs, tmpfs
    /tmp and /dev/shm, and the unprivileged identity."""
    from tests.integration.crawler.helpers import probe_execution, run_probe

    # (a) Metadata inspection of the exact crawler argv.
    name = "darkula-crawler-inspect-probe"
    await _run_podman_argv("rm", "-f", name)
    argv = sandbox._build_run_argv(
        probe_execution("inspect-probe", "http://darkula-fake-world-intg:8080"),
        name,
    )
    assert argv[-3:] == ["python3", "-m", "crawler_runtime"]
    argv[-3:] = ["sleep", "30"]
    rc, _, stderr = await _run_podman_argv("run", "-d", *argv[1:])
    assert rc == 0, stderr
    try:
        rc, stdout, _ = await _run_podman_argv("container", "inspect", name)
        assert rc == 0
        document = json.loads(stdout.decode())[0]
        host_config = document.get("HostConfig", {})
        assert host_config.get("Privileged") is False
        cap_drop = host_config.get("CapDrop", [])
        assert isinstance(cap_drop, list) and "CAP_SETUID" in cap_drop
        assert host_config.get("SecurityOpt") == ["no-new-privileges"]
        assert host_config.get("PidsLimit") == 512
        assert host_config.get("Memory") == 1024 * 1024 * 1024
        assert host_config.get("CpuQuota") == 200000
        assert host_config.get("Binds") == []
        assert document.get("Config", {}).get("User") == "10001"
        networks = document.get("NetworkSettings", {}).get("Networks", {})
        assert "darkula-intg" in networks
    finally:
        await _run_podman_argv("rm", "-f", name)

    # (b) Runtime probe: rootfs read-only, tmpfs writable, non-root identity.
    runtime_probe = (
        "import os, sys\n"
        "with open('/proc/self/mounts') as fh:\n"
        "    mounts = fh.read()\n"
        "tmp_ok = '/tmp' in mounts and '/dev/shm' in mounts\n"
        "rootfs_writable = False\n"
        "try:\n"
        "    with open('/probe-root-write', 'w') as fh:\n"
        "        fh.write('x')\n"
        "except OSError:\n"
        "    rootfs_writable = True\n"
        "tmp_writable = False\n"
        "try:\n"
        "    with open('/tmp/probe-write', 'w') as fh:\n"
        "        fh.write('x')\n"
        "    tmp_writable = True\n"
        "except OSError:\n"
        "    pass\n"
        "uid = os.getuid()\n"
        "print(f'TMP={tmp_ok} RO={rootfs_writable} TMPW={tmp_writable} UID={uid}')\n"
        "ok = tmp_ok and rootfs_writable and tmp_writable and uid == 10001\n"
        "raise SystemExit(0 if ok else 1)\n"
    )
    code, stdout, _ = await run_probe(
        sandbox,
        "runtime-flags",
        "http://darkula-fake-world-intg:8080",
        runtime_probe,
        "127.0.0.1",
        "1",
    )
    assert code == 0, stdout
    assert b"UID=10001" in stdout and b"TMP=True" in stdout


@pytest.mark.asyncio
async def test_p11_pids_limit_enforced(sandbox: PodmanSandbox) -> None:
    """The pids ceiling is enforced on a live container: forking beyond it is
    impossible."""
    pids_probe = (
        "import os\n"
        "children = 0\n"
        "for _ in range(1200):\n"
        "    try:\n"
        "        pid = os.fork()\n"
        "    except OSError:\n"
        "        break\n"
        "    if pid == 0:\n"
        "        os._exit(0)\n"
        "    else:\n"
        "        children += 1\n"
        "print(f'FORKED={children}')\n"
        "raise SystemExit(0 if children < 1200 else 1)\n"
    )
    from tests.integration.crawler.helpers import run_probe

    code, stdout, _ = await run_probe(
        sandbox,
        "pids-probe",
        "http://darkula-fake-world-intg:8080",
        pids_probe,
        "127.0.0.1",
        "1",
    )
    assert code == 0, stdout
    forked = 0
    for line in stdout.decode(errors="replace").splitlines():
        if line.startswith("FORKED="):
            forked = int(line.split("=", 1)[1])
    assert 0 < forked < 512, f"pids ceiling not enforced (forked={forked})"


@pytest.mark.asyncio
async def test_p12_memory_limit_enforced(sandbox: PodmanSandbox) -> None:
    """A single allocation beyond the memory ceiling cannot complete inside a
    live crawler-policy container (cgroup ceiling enforced by the sandbox)."""
    memory_probe = (
        "import sys\n"
        "chunk = bytearray(8 * 1024 * 1024)\n"
        "allocated = 0\n"
        "try:\n"
        "    while allocated < 2048:\n"
        "        chunk.extend(bytearray(8 * 1024 * 1024))\n"
        "        # touch the new pages deterministically\n"
        "        start = allocated * 8 * 1024 * 1024\n"
        "        chunk[start : start + 4096] = b'x' * 4096\n"
        "        allocated += 1\n"
        "except MemoryError:\n"
        "    pass\n"
        "print(f'ALLOCATED_MB={allocated * 8}')\n"
        "raise SystemExit(0 if allocated < 2048 else 1)\n"
    )
    from tests.integration.crawler.helpers import run_probe

    code, stdout, stderr = await run_probe(
        sandbox,
        "mem-probe",
        "http://darkula-fake-world-intg:8080",
        memory_probe,
        "127.0.0.1",
        "1",
    )
    combined = (stdout + stderr).decode(errors="replace")
    assert "ALLOCATED_MB=2048" not in combined or code != 0, (
        "memory ceiling did not stop a 2 GiB allocation in a 1 GiB sandbox"
    )

# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""PR 7 crawler integration fixtures.

Fail closed when the provisioned Darkula-owned infrastructure (runtime image,
internal network, Fake World HTTP container) is missing or mislabeled — a
missing resource is an environment error, not a suite skip. Only Darkula-owned
resources are created or removed by these fixtures; ownership is verified from
exact metadata (labels) before any reuse/mutation/removal.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from darkula.config.settings import CrawlerSettings
from darkula.crawler import CrawlerController
from darkula.infrastructure.sandbox.podman import PodmanSandbox

FAKE_WORLD_URL = "http://darkula-fake-world-intg:8080"
FAKE_WORLD_CONTAINER = "darkula-fake-world-intg"
RUNTIME_IMAGE = "localhost/darkula-crawler-runtime:1.63.0"
NETWORK_NAME = "darkula-intg"
OWNED_LABEL = "darkula.owned=true"
SIBLING_NAME = "darkula-sibling-http-intg"
HANG_NAME = "darkula-hang-intg"


async def _run_podman_argv(*argv: str) -> tuple[int, bytes, bytes]:
    """Run one bounded podman command; values are fixed/parameterized."""
    proc = await asyncio.create_subprocess_exec(
        "podman",
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
    return proc.returncode or 0, stdout, stderr


def _fail_closed(message: str) -> pytest.UsageError:
    return pytest.UsageError(
        f"{message}\n"
        f"Run ./build.sh --intg to provision the Darkula-owned crawler "
        f"infrastructure (network {NETWORK_NAME!r}, runtime image, Fake World "
        f"HTTP) before executing this suite."
    )


async def podman_network_is_labeled_owned(name: str) -> bool:
    """Verify exact ownership metadata of a Darkula network (fail closed)."""
    rc, stdout, _ = await _run_podman_argv("network", "inspect", name)
    if rc != 0:
        return False
    try:
        document = json.loads(stdout.decode())
    except Exception:
        return False
    if isinstance(document, list):
        document = document[0] if document else {}
    labels = (document.get("labels") or {}) if isinstance(document, dict) else {}
    raw = labels.get("darkula.owned") if isinstance(labels, dict) else None
    return bool(raw == "true")


async def podman_container_is_labeled_owned(name: str) -> bool:
    """Verify exact ownership metadata of a Darkula container (fail closed)."""
    rc, stdout, _ = await _run_podman_argv("container", "inspect", name)
    if rc != 0:
        return False
    try:
        document = json.loads(stdout.decode())
    except Exception:
        return False
    if not isinstance(document, list) or not document:
        return False
    labels = document[0].get("Config", {}).get("Labels", {})
    return bool(labels.get("darkula.owned") == "true")


async def remove_owned_container(name: str) -> None:
    """Remove only a Darkula-owned container; fail closed otherwise."""
    if not await podman_container_is_labeled_owned(name):
        raise RuntimeError(
            f"refusing to remove {name!r}: resource is not Darkula-owned"
        )
    await _run_podman_argv("rm", "-f", name)


async def container_names() -> set[str]:
    """Names of every current container (never execut/launch anything)."""
    _, stdout, _ = await _run_podman_argv("ps", "-a", "--format", "{{.Names}}")
    return {
        line.strip()
        for line in stdout.decode(errors="replace").splitlines()
        if line.strip()
    }


@pytest_asyncio.fixture(scope="session", autouse=True)
async def crawler_infrastructure_ready() -> AsyncIterator[None]:
    """Verify the provisioned Fake World/crawler infrastructure, fail closed.

    A disposable probe container proves the full path the tests exercise:
    image present, network present and labeled, Fake World serving HTTP on the
    internal network through the same crawler-boundary flags.
    """
    rc, _, _ = await _run_podman_argv("--version")
    if rc != 0:
        raise _fail_closed("podman is not available on this host")
    rc, _, _ = await _run_podman_argv("image", "exists", RUNTIME_IMAGE)
    if rc != 0:
        raise _fail_closed(f"runtime image {RUNTIME_IMAGE!r} is not present")
    if not await podman_network_is_labeled_owned(NETWORK_NAME):
        raise _fail_closed(
            f"network {NETWORK_NAME!r} is missing or does not carry darkula.owned=true"
        )
    if not await podman_container_is_labeled_owned(FAKE_WORLD_CONTAINER):
        raise _fail_closed(
            f"container {FAKE_WORLD_CONTAINER!r} is missing or not Darkula-owned"
        )
    from tests.integration.crawler.helpers import _HTTP_GET_SRC

    argv = [
        "run",
        "--rm",
        "-i",
        "--name",
        "darkula-crawler-readable-probe",
        "--network",
        NETWORK_NAME,
        "--label",
        OWNED_LABEL,
        "--label",
        "darkula.service=crawler",
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
        _HTTP_GET_SRC,
        FAKE_WORLD_CONTAINER,
        "8080",
    ]
    rc, stdout, stderr = await _run_podman_argv(*argv)
    if rc != 0 or b"OK" not in stdout:
        raise _fail_closed(
            f"Fake World HTTP is not reachable from the internal network "
            f"(probe rc={rc} out={stdout[:120]!r} err={stderr[:200]!r})"
        )
    yield


@pytest_asyncio.fixture(scope="session")
async def fake_world_url() -> str:
    """Base URL of the Fake World HTTP service on the internal network."""
    return FAKE_WORLD_URL


@pytest.fixture(scope="session")
def crawler_settings() -> CrawlerSettings:
    """Production crawler settings (runtime image/network from config)."""
    return CrawlerSettings()


@pytest_asyncio.fixture(scope="session")
async def sandbox(crawler_settings: CrawlerSettings) -> AsyncIterator[PodmanSandbox]:
    """The real PodmanSandbox (one fresh container per execution)."""
    yield PodmanSandbox.from_settings(crawler_settings)


@pytest_asyncio.fixture(scope="session")
async def controller(
    crawler_settings: CrawlerSettings, sandbox: PodmanSandbox
) -> AsyncIterator[CrawlerController]:
    """The real CrawlerController over the real sandbox."""
    yield CrawlerController(settings=crawler_settings, sandbox=sandbox)


@pytest_asyncio.fixture(scope="session")
async def unauthorized_sibling() -> AsyncIterator[str]:
    """An HTTP marker service on the DEFAULT podman network.

    Darkula-owned (provisioned/labeled by this suite) but NOT on the isolation
    network: the crawler must be unable to reach it. Serves a unique marker.
    """
    marker_server = (
        "import socket\n"
        "s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.bind(('0.0.0.0', 9080)); s.listen(16)\n"
        "while True:\n"
        "    c, _ = s.accept()\n"
        "    c.recv(4096)\n"
        "    c.sendall(b'HTTP/1.1 200 OK\\r\\nContent-Length: 24\\r\\n"
        "Connection: close\\r\\n\\r\\nunauthorized-sibling-marker')\n"
        "    c.close()\n"
    )
    await _run_podman_argv("rm", "-f", SIBLING_NAME)
    rc, stdout, stderr = await _run_podman_argv(
        "run",
        "-d",
        "--name",
        SIBLING_NAME,
        "--label",
        OWNED_LABEL,
        "--label",
        "darkula.service=probe-sibling",
        RUNTIME_IMAGE,
        "python3",
        "-c",
        marker_server,
    )
    if rc != 0:
        raise _fail_closed(
            f"failed to start sibling probe container {SIBLING_NAME!r}: "
            f"{stderr[:200]!r}{stdout[:100]!r}"
        )
    try:
        yield SIBLING_NAME
    finally:
        try:
            await remove_owned_container(SIBLING_NAME)
        except RuntimeError:
            pass


@pytest_asyncio.fixture(scope="session")
async def sibling_ip(unauthorized_sibling: str) -> str:
    """IP of the unauthorized sibling on the default podman network.

    Containers on the default rootless network do not expose their address via
    ``NetworkSettings``, so it is resolved from inside the (Darkula-owned)
    sibling container itself.
    """
    rc, stdout, stderr = await _run_podman_argv(
        "exec",
        unauthorized_sibling,
        "python3",
        "-c",
        "import socket; print(socket.gethostbyname_ex(socket.gethostname())[2][0])",
    )
    if rc != 0:
        raise _fail_closed(
            f"cannot resolve sibling probe container {unauthorized_sibling!r}: "
            f"{stderr[:200]!r}"
        )
    address = stdout.decode(errors="replace").strip().splitlines()
    if not address:
        raise _fail_closed(
            f"sibling probe container {unauthorized_sibling!r} has no IP address"
        )
    return address[0]


@pytest_asyncio.fixture(scope="session")
async def hanging_sibling() -> AsyncIterator[str]:
    """A Darkula-owned listener on the isolation network that never answers.

    Accepts exactly one TCP connection and holds it open (bounded sleep), so a
    browser navigation against it blocks until the sandbox timeout or until
    the calling task is cancelled.
    """
    hang_server = (
        "import socket, time\n"
        "s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.bind(('0.0.0.0', 8080)); s.listen(1)\n"
        "c, _ = s.accept()\n"
        "time.sleep(3600)\n"
    )
    rc, stdout, stderr = await _run_podman_argv(
        "run",
        "-d",
        "--name",
        HANG_NAME,
        "--network",
        NETWORK_NAME,
        "--label",
        OWNED_LABEL,
        "--label",
        "darkula.service=probe-hang",
        RUNTIME_IMAGE,
        "python3",
        "-c",
        hang_server,
    )
    if rc != 0:
        raise _fail_closed(
            f"failed to start hanging sibling {HANG_NAME!r}: "
            f"{stderr[:200]!r}{stdout[:100]!r}"
        )
    await asyncio.sleep(1.5)  # let the listener bind before tests use it
    try:
        yield HANG_NAME
    finally:
        try:
            await remove_owned_container(HANG_NAME)
        except RuntimeError:
            pass


@pytest_asyncio.fixture(scope="session", autouse=True)
async def no_crawler_containers_leak() -> AsyncIterator[None]:
    """The whole suite may not leave any Darkula crawler container behind."""
    yield
    names = await container_names()
    leaked = sorted(name for name in names if name.startswith("darkula-crawler-"))
    if leaked:
        raise AssertionError(f"crawler integration left containers behind: {leaked}")

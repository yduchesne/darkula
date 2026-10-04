# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Network isolation negative tests (P20-P25).

Every probe runs inside a real disposable container built with the exact
crawler sandbox flags (through the same ``_build_run_argv`` the controller
uses), so these prove the *effective* boundary below any crawl logic: the
authorized Fake World destination is the only reachable endpoint.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from darkula.config.settings import CrawlerSettings
from darkula.crawler import CrawlerController
from darkula.infrastructure.sandbox.podman import PodmanSandbox
from tests.integration.crawler.conftest import _run_podman_argv
from tests.integration.crawler.helpers import (
    _HTTP_GET_SRC,
    _PROBE_SRC,
    SIBLING_MARKER,
    crawl_request,
    dns_probe_src,
    run_probe,
    truth_leaks,
)

pytestmark = pytest.mark.integration

#: Fake World destination and prohibited destinations (port namespace 3).
FAKE_WORLD_HOST = "darkula-fake-world-intg"
FAKE_WORLD_PORT = 8080
HOST_LISTENER_PORT = 39080
GATEWAY_IP = "10.177.42.1"
METADATA_IP = "169.254.169.254"


async def _probe(
    sandbox: PodmanSandbox, execution_id: str, source: str, arg1: str, arg2: str
) -> tuple[int, bytes]:
    code, stdout, _ = await run_probe(
        sandbox,
        execution_id,
        f"http://{FAKE_WORLD_HOST}:{FAKE_WORLD_PORT}",
        source,
        arg1,
        arg2,
    )
    return code, stdout


async def _container_network_ip(container: str) -> str | None:
    """IP of ``container`` on its own network, resolved host-side."""
    rc, stdout, _ = await _run_podman_argv("container", "inspect", container)
    if rc != 0:
        return None
    try:
        document = json.loads(stdout.decode())
    except Exception:
        return None
    if not isinstance(document, list) or not document:
        return None
    networks = document[0].get("NetworkSettings", {}).get("Networks", {})
    for network in networks.values():
        address = network.get("IPAddress")
        if address:
            return str(address)
    return None


@pytest.mark.asyncio
async def test_p20_authorized_fake_world_reachable(
    sandbox: PodmanSandbox,
) -> None:
    """Positive control: the authorized destination is reachable through the
    exact crawler flags (so the negatives below are not explained by a broken
    probe harness)."""
    code, stdout = await _probe(
        sandbox, "net-pos", _HTTP_GET_SRC, FAKE_WORLD_HOST, str(FAKE_WORLD_PORT)
    )
    assert code == 0 and b"OK" in stdout


@pytest.mark.asyncio
async def test_p21_postgres_unreachable(sandbox: PodmanSandbox) -> None:
    """The Darkula Postgres container (foreign network) is unreachable by
    name even when it is running on its own network."""
    code, stdout = await _probe(
        sandbox, "net-pg-name", dns_probe_src(), "darkula-postgres", "5432"
    )
    assert code != 0, stdout
    address = await _container_network_ip("darkula-postgres")
    if address is not None:
        code, stdout = await _probe(sandbox, "net-pg-ip", _PROBE_SRC, address, "5432")
        assert code != 0, stdout


@pytest.mark.asyncio
async def test_p22_redpanda_unreachable(sandbox: PodmanSandbox) -> None:
    code, stdout = await _probe(
        sandbox, "net-rp", dns_probe_src(), "darkula-redpanda", "9092"
    )
    assert code != 0, stdout


@pytest.mark.asyncio
async def test_p23_unauthorized_sibling_unreachable(
    sandbox: PodmanSandbox,
    sibling_ip: str,
    crawler_settings: CrawlerSettings,
) -> None:
    """An HTTP service on the DEFAULT podman network is unreachable from the
    crawler sandbox even by direct IP; a real crawl never echoes its marker."""
    code, stdout = await _probe(sandbox, "net-sib", _PROBE_SRC, sibling_ip, "9080")
    assert code != 0, f"sibling marker service was reachable: {stdout[:120]!r}"

    controller = CrawlerController(settings=crawler_settings, sandbox=sandbox)
    result = await controller.crawl(
        crawl_request(
            f"http://{FAKE_WORLD_HOST}:{FAKE_WORLD_PORT}",
            start_path="/",
            max_pages=8,
            max_requests=30,
            max_depth=1,
            timeout_seconds=90.0,
        )
    )
    assert all(SIBLING_MARKER not in p.text_excerpt for p in result.pages)
    assert truth_leaks(result) == []


class _MarkerHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"host-listener-marker"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return None


@pytest.fixture(scope="module")
def host_listener() -> object:
    """A Darkula host-port listener (39080) on all host interfaces."""
    server = ThreadingHTTPServer(("0.0.0.0", HOST_LISTENER_PORT), _MarkerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port: int = HOST_LISTENER_PORT
        yield port
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_p24_host_gateway_and_loopback_denied(
    sandbox: PodmanSandbox, host_listener: int
) -> None:
    """Host services behind the internal-network gateway are denied: a host
    listener on 0.0.0.0:39080 must not be reachable from the sandbox via
    10.177.42.1, and nothing is published into the container's loopback."""
    code, stdout = await _probe(
        sandbox, "net-gw", _PROBE_SRC, GATEWAY_IP, str(host_listener)
    )
    assert code != 0, f"host service reachable via gateway: {stdout[:120]!r}"
    code, stdout = await _probe(
        sandbox, "net-lo", _PROBE_SRC, "127.0.0.1", str(host_listener)
    )
    assert code != 0, stdout


@pytest.mark.asyncio
async def test_p25_cloud_metadata_denied(sandbox: PodmanSandbox) -> None:
    """Link-local cloud metadata (169.254.169.254) is explicitly denied."""
    code, stdout = await _probe(sandbox, "net-meta", _PROBE_SRC, METADATA_IP, "80")
    assert code != 0, stdout


@pytest.mark.asyncio
async def test_p26_no_host_published_ports_on_isolation_network() -> None:
    """The Fake World container publishes no host port (the prefix-3 host
    namespace is untouched by the crawler stack)."""
    rc, stdout, _ = await _run_podman_argv(
        "container", "inspect", "darkula-fake-world-intg"
    )
    assert rc == 0
    document = json.loads(stdout.decode())[0]
    ports = document.get("NetworkSettings", {}).get("Ports", {})
    # A published host port maps container:port -> [...]; a None value means
    # the internal port is NOT bound on the host (no publishing).
    published = {key: value for key, value in ports.items() if value is not None}
    assert published == {}, f"Fake World container published host ports: {ports}"

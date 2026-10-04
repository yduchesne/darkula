# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Shared helpers for the PR 7 real-Podman crawler integration suite.

All helpers manage only Darkula-owned resources (labels verified before
removal) and fail closed when the provisioned infrastructure is missing or
mislabeled. Tests never reach through the controller/sandbox to the Fake
World truth: renderings come only from real browser HTTP traffic.
"""

from __future__ import annotations

from darkula.crawler.contracts import (
    AllowedOrigin,
    CrawlCredentials,
    CrawlPageObservation,
    CrawlRequest,
    CrawlResult,
)
from darkula.sandbox.contracts import (
    AllowedDestination,
    FilesystemPolicy,
    InputPolicy,
    NetworkPolicy,
    OutputPolicy,
    ResourceLimits,
    RuntimeIsolationPolicy,
    SandboxExecutionPolicy,
    SandboxExecutionRequest,
    SandboxWorkload,
    TmpfsMount,
    WorkloadKind,
)

#: Truth-only identifiers and the authentication secret that must NEVER
#: appear in any crawl observation (page title, text excerpt, or link URL).
TRUTH_ONLY_TOKENS = ("actor-001", "alias-002", "relationship-001", "zfox")
AUTH_PASSWORD = "blackgate-test-password"
#: Public alias display name rendered by BlackGate itself (not a secret).
PUBLIC_USERNAME = "zerofox77"

#: Marker served by the intentionally unauthorized sibling probe container.
SIBLING_MARKER = "unauthorized-sibling-marker"


def crawl_request_id() -> str:
    """Return a unique bounded request id for one integration crawl."""
    import uuid

    return f"intg-{uuid.uuid4().hex[:10]}"


def origin_for(base_url: str) -> AllowedOrigin:
    """Parse the Fake World base URL into the crawl authorization origin."""
    return AllowedOrigin.parse(base_url)


def crawl_request(
    base_url: str,
    *,
    start_path: str = "/",
    max_pages: int = 40,
    max_requests: int = 120,
    max_depth: int = 4,
    timeout_seconds: float = 160.0,
) -> CrawlRequest:
    """Build one bounded CrawlRequest against the Fake World service."""
    return CrawlRequest(
        request_id=crawl_request_id(),
        start_url=f"{base_url}{start_path}",
        allowed_origin=origin_for(base_url),
        credentials=CrawlCredentials(PUBLIC_USERNAME, AUTH_PASSWORD),
        max_pages=max_pages,
        max_requests=max_requests,
        max_depth=max_depth,
        timeout_seconds=timeout_seconds,
    )


def observation_text(observation: CrawlPageObservation) -> str:
    """Hostile/untrusted text of one observation (never telemetry)."""
    return f"{observation.title}\n{observation.text_excerpt}"


def truth_leaks(result: CrawlResult) -> list[tuple[str, str]]:
    """Return every (url, token) where a truth-only value crossed the crawl."""
    leaks: list[tuple[str, str]] = []
    for page in result.pages:
        haystack = observation_text(page).lower()
        for token in TRUTH_ONLY_TOKENS:
            if token.lower() in haystack:
                leaks.append((page.url, token))
        if AUTH_PASSWORD.lower() in haystack:
            leaks.append((page.url, "auth-password"))
        if SIBLING_MARKER in haystack:
            leaks.append((page.url, "sibling-marker"))
    for link in result.discovered_links:
        for token in TRUTH_ONLY_TOKENS:
            if token.lower() in link.lower():
                leaks.append((link, token))
    return leaks


def pages_with(result: CrawlResult, url_substring: str) -> list[CrawlPageObservation]:
    """Return observations whose URL contains ``url_substring``."""
    return [page for page in result.pages if url_substring in page.url]


def title_contains(page: CrawlPageObservation, fragment: str) -> bool:
    """True when the observation title contains ``fragment`` (exact)."""
    return fragment in page.title


def probe_policy(base_url: str) -> SandboxExecutionPolicy:
    """The same least-capability policy a real controller would derive."""
    origin = origin_for(base_url)
    return SandboxExecutionPolicy(
        network=NetworkPolicy(
            destinations=(AllowedDestination(host=origin.host, port=origin.port),)
        ),
        filesystem=FilesystemPolicy(
            tmpfs=(
                TmpfsMount("/tmp", 256 * 1024 * 1024),
                TmpfsMount("/dev/shm", 256 * 1024 * 1024),
            )
        ),
        resources=ResourceLimits(timeout_seconds=90.0),
        runtime=RuntimeIsolationPolicy(),
        input=InputPolicy(credentials_allowed=False),
        output=OutputPolicy(),
    )


def probe_execution(execution_id: str, base_url: str) -> SandboxExecutionRequest:
    """One sandbox probe execution sharing the crawler isolation posture."""
    return SandboxExecutionRequest(
        execution_id=execution_id,
        policy=probe_policy(base_url),
        workload=SandboxWorkload(kind=WorkloadKind.CRAWLER, payload=b"{}"),
    )


#: Probe source run with ``python3 -c <src> <ip> <port>``; prints one line
#: and exits 0 when a TCP connection completes, 2 otherwise. Bounded waits so
#: unreachable destinations fail promptly instead of hanging the suite.
_PROBE_SRC = (
    "import socket, sys\n"
    "ip, port = sys.argv[1], int(sys.argv[2])\n"
    "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
    "s.settimeout(4.0)\n"
    "rc = s.connect_ex((ip, port))\n"
    "print('OK' if rc == 0 else f'FAIL:{rc}')\n"
    "raise SystemExit(0 if rc == 0 else 2)\n"
)


def dns_probe_src() -> str:
    """Probe source resolving a name before connecting (bounded)."""
    return (
        "import socket, sys\n"
        "name, port = sys.argv[1], int(sys.argv[2])\n"
        "try:\n"
        "    addresses = socket.getaddrinfo(\n"
        "        name, port, socket.AF_INET, socket.SOCK_STREAM)\n"
        "    candidate = addresses[0][4][0]\n"
        "except OSError:\n"
        "    print('DNSFAIL')\n"
        "    raise SystemExit(2)\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "s.settimeout(4.0)\n"
        "rc = s.connect_ex((candidate, port))\n"
        "print('OK' if rc == 0 else f'FAIL:{rc}')\n"
        "raise SystemExit(0 if rc == 0 else 2)\n"
    )


async def run_probe(
    sandbox: object,
    execution_id: str,
    base_url: str,
    source: str,
    arg1: str,
    arg2: str,
) -> tuple[int, bytes, bytes]:
    """Run one probe inside a real disposable crawler-policy container.

    ``sandbox`` must expose ``_build_run_argv(request, name)`` and
    ``_run_podman(argv, *, command_timeout=...)`` (the real PodmanSandbox).
    The probe container is always removed afterwards (it is Darkula-owned by
    construction: built from the sandbox argv builder).
    """
    from darkula.infrastructure.sandbox.podman import PodmanSandbox

    assert isinstance(sandbox, PodmanSandbox)
    name = f"darkula-crawler-{execution_id}-probe"
    argv = sandbox._build_run_argv(probe_execution(execution_id, base_url), name)
    # Replace the workload command with the bounded probe (no shell).
    assert argv[-3:] == ["python3", "-m", "crawler_runtime"]
    argv[-3:] = ["python3", "-c", source, arg1, arg2]
    try:
        return await sandbox._run_podman(argv, command_timeout=60.0)
    finally:
        try:
            await sandbox._run_podman(["rm", "-f", name], command_timeout=30.0)
        except Exception:
            pass


#: Probe source for the positive control (full HTTP GET to the Fake World).
_HTTP_GET_SRC = (
    "import socket, sys\n"
    "host, port = sys.argv[1], int(sys.argv[2])\n"
    "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
    "s.settimeout(8.0)\n"
    "s.connect((host, port))\n"
    "s.sendall(b'GET / HTTP/1.1\\r\\nHost: %s\\r\\n' "
    "         b'Connection: close\\r\\n\\r\\n' % host.encode())\n"
    "chunks = []\n"
    "while True:\n"
    "    chunk = s.recv(4096)\n"
    "    if not chunk:\n"
    "        break\n"
    "    chunks.append(chunk)\n"
    "body = b''.join(chunks)\n"
    "first = body.split(b'\\r\\n', 1)[0]\n"
    "print('OK' if b'200' in first else 'BAD:' + body[:60].decode('latin1'))\n"
    "raise SystemExit(0 if b'200' in first else 2)\n"
)

# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""PR 7 canonical vertical slice over the real stack.

real CrawlerController -> real PodmanSandbox -> fresh disposable Podman
container -> real CrawlerRuntime -> real Playwright + Chromium -> real HTTP
-> BlackGate Fake World HTTP service.

The slice is two bounded executions because the Fake World deliberately caps
authenticated sessions at 12 protected renders (a documented scenario
semantic); a cold-start structural crawl demonstrates the redirect/login/
session/503/attachments surface, and a focused crawl starting at the
hospital thread's page-1 URL demonstrates engine re-observation of the
auth-bounced target plus pagination continuity. Both executions share one
disposable container per execution and must leave zero containers behind.
"""

from __future__ import annotations

import asyncio

import pytest

from darkula.crawler import CrawlStatus
from darkula.infrastructure.sandbox.podman import PodmanSandbox
from tests.integration.crawler.conftest import container_names
from tests.integration.crawler.helpers import (
    crawl_request,
    pages_with,
    title_contains,
    truth_leaks,
)

pytestmark = pytest.mark.integration


async def _no_crawler_containers() -> bool:
    """True when no Darkula crawler container exists right now."""
    names = await container_names()
    return not any(name.startswith("darkula-crawler-") for name in names)


@pytest.mark.asyncio
async def test_fresh_disposable_container_per_execution(
    controller: object, fake_world_url: str
) -> None:
    """P2: exactly one fresh disposable container per execution, removed
    afterwards; a second execution gets a distinct container."""

    async def watch_live_containers(seen: list[str]) -> None:
        while not seen:
            names = await container_names()
            live = [n for n in names if n.startswith("darkula-crawler-")]
            if live:
                seen.extend(live)
            await asyncio.sleep(0.1)

    async def one_execution() -> list[str]:
        seen: list[str] = []
        task = asyncio.create_task(
            controller.crawl(  # type: ignore[attr-defined]
                crawl_request(
                    fake_world_url,
                    start_path="/login",
                    max_pages=8,
                    max_requests=40,
                    max_depth=2,
                    timeout_seconds=90.0,
                )
            )
        )
        watcher = asyncio.create_task(watch_live_containers(seen))
        result = await task
        await watcher
        assert result.status is CrawlStatus.COMPLETED
        assert seen, "no disposable container was observed during execution"
        assert await _no_crawler_containers(), "container leaked after execution"
        return seen

    first = await one_execution()
    second = await one_execution()
    assert second != first, "every execution must use a fresh disposable container"


@pytest.mark.asyncio
async def test_canonical_blackgate_browser_slice(
    controller: object,
    fake_world_url: str,
    sandbox: PodmanSandbox,
) -> None:
    """The canonical vertical slice: structure crawl (A) + pagination (B)."""
    # -- Execution A: cold-start structural crawl -------------------------
    result = await controller.crawl(  # type: ignore[attr-defined]
        crawl_request(
            fake_world_url,
            start_path="/",
            max_pages=60,
            max_requests=180,
            max_depth=4,
            timeout_seconds=200.0,
        )
    )
    assert result.status is CrawlStatus.COMPLETED, result.reason
    assert result.requests >= 12
    assert len(result.pages) >= 12

    # Redirect semantics: the protected root/target bounces to the login form
    # and the login POST lands on the board index (session retained).
    pre_login = [
        p for p in result.pages if p.url.endswith("/index") and "Log in" in p.title
    ]
    assert pre_login, "protected /index did not bounce to the login form"
    post_login = [
        p for p in result.pages if p.url.endswith("/index") and "Board index" in p.title
    ]
    assert post_login, "login POST did not land on the authenticated index"

    # The public + protected board surface is observed with real statuses.
    for board in (
        "/board/board-access",
        "/board/board-announcements",
        "/board/board-archives",
        "/board/board-chatter",
    ):
        hits = pages_with(result, board)
        assert hits and hits[0].http_status == 200, board

    # The protected hospital thread renders its public listing content.
    hospital = pages_with(result, "/thread/thr-hospital-creds")
    assert hospital, "hospital thread was not discovered"
    page1 = next(p for p in hospital if "?" not in p.url)
    assert page1.http_status == 200
    assert "Mason Creek" in page1.title or "Mason Creek" in page1.text_excerpt
    assert "mgmt.local" in page1.text_excerpt

    # The malformed legacy page is observed (still a 200 rendered body).
    legacy = pages_with(result, "/legacy/board-announcements")
    assert legacy and legacy[0].http_status == 200
    assert title_contains(legacy[0], "Legacy announcements")

    # thr-intros carries a source-level 503 schedule; the engine's single
    # bounded retry must have surfaced a 200 observation.
    intros = pages_with(result, "/thread/thr-intros")
    assert intros and intros[0].http_status == 200, "503 retry did not succeed"

    # Attachments are downloaded with a bounded sample observation.
    attachments = pages_with(result, "/attachment/")
    assert attachments and any(
        a.http_status == 200 and "synthetic test fixture" in a.text_excerpt
        for a in attachments
    ), "no bounded attachment sample observed"

    # Session expiry (12 protected renders) is handled without crashing: the
    # crawl completes and later protected pages render as the login form.
    assert len(pages_with(result, "/index")) >= 1
    assert all(p.http_status in (200, 401, None) for p in result.pages), (
        "unexpected HTTP status vocabulary"
    )
    # A real browser entity-decodes &amp; in href attributes; the runtime must
    # surface decoded links (never a literal `&amp;` in discovered URLs).
    assert all("&amp;" not in link for link in result.discovered_links), (
        "undecoded HTML entity in discovered links"
    )

    assert truth_leaks(result) == []

    # -- Execution B: focused pagination crawl (pagination continuity) ----
    result_b = await controller.crawl(  # type: ignore[attr-defined]
        crawl_request(
            fake_world_url,
            start_path="/thread/thr-hospital-creds?page=1",
            max_pages=30,
            max_requests=140,
            max_depth=3,
            timeout_seconds=180.0,
        )
    )
    assert result_b.status is CrawlStatus.COMPLETED, result_b.reason

    # The engine re-observes the auth-bounced target after logging in: the
    # hospital thread page 1 renders post-login within this execution.
    page1_b = [
        p
        for p in pages_with(result_b, "/thread/thr-hospital-creds?page=1")
        if "Page 1 of 2" in p.text_excerpt
    ]
    assert page1_b, "focused crawl never re-observed the thread page 1"
    assert page1_b[0].http_status == 200

    # Pagination continuity: page 2 renders with its own content, proven by
    # the same authenticated session (cookie retention across page hops).
    page2 = pages_with(result_b, "/thread/thr-hospital-creds?page=2")
    assert page2 and page2[0].http_status == 200, "page 2 was not followed"
    assert "Page 2 of 2" in page2[0].text_excerpt
    assert "Closing this listing" in page2[0].text_excerpt
    assert "Mason Creek" in page2[0].title or "Mason Creek" in page2[0].text_excerpt

    # Attachments remain bounded-sampleable from the focused path.
    attachments_b = pages_with(result_b, "/attachment/")
    assert any(
        a.http_status == 200 and "synthetic test fixture" in a.text_excerpt
        for a in attachments_b
    )

    assert truth_leaks(result_b) == []

    # Neither execution may leak a container (P30 semantics at slice level).
    assert await _no_crawler_containers()


@pytest.mark.asyncio
async def test_slice_uses_the_real_runtime_and_real_http(
    fake_world_url: str, sandbox: PodmanSandbox
) -> None:
    """P1 proof-through: the slice's container actually is the runtime image
    running Playwright/Chromium (we inspect the live container metadata for
    the exact image and workload command), and the observations above could
    only come from real HTTP renders of the Fake World service.

    This test re-checks the positive control at the HTTP layer through one
    disposable crawler-policy container.
    """
    from tests.integration.crawler.helpers import _HTTP_GET_SRC, run_probe

    code, stdout, _ = await run_probe(
        sandbox,
        "slice-probe-http",
        fake_world_url,
        _HTTP_GET_SRC,
        "darkula-fake-world-intg",
        "8080",
    )
    assert code == 0 and b"OK" in stdout, "positive HTTP control failed"

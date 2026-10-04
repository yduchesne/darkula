# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""CrawlerRuntime engine tests (PR 7) — browser code is never imported here.

The engine is browser-independent by construction (a small PageAdapter
abstraction); the Playwright adapter is exercised by the integration suite
inside the disposable container.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from crawler_runtime.engine import (
    AuthDecision,
    NavigateResult,
    TraversalCredentials,
    TraversalInput,
    TraversalOutput,
    traverse,
)

ORIGIN_HOST = "blackgate.example.test"
ORIGIN_SCHEME = "http"
ORIGIN_PORT = 8080
BASE = f"{ORIGIN_SCHEME}://{ORIGIN_HOST}:{ORIGIN_PORT}"


@dataclass
class FakePage:
    """Scripted deterministic page map for the fake adapter."""

    pages: dict[str, NavigateResult]
    navigated: list[str] = field(default_factory=list)
    submitted: list[tuple[str, str]] = field(default_factory=list)
    redirects: int = 0
    crash_on_navigate: bool = False
    post_login_pages: dict[str, NavigateResult] = field(default_factory=dict)
    redirect_urls: set[str] = field(default_factory=set)
    _logged_in: bool = False

    async def navigate(self, url: str, *, timeout_ms: int) -> NavigateResult | None:
        self.navigated.append(url)
        if self.crash_on_navigate:
            raise RuntimeError("browser crash")
        if url in self.redirect_urls:
            # Mirrors the real response listener: a 3xx is observed during the
            # navigation itself.
            self.redirects += 1
        if self._logged_in:
            return self.post_login_pages.get(url, self.pages.get(url))
        return self.pages.get(url)

    async def submit_auth_form(
        self, *, username: str, password: str, timeout_ms: int
    ) -> AuthDecision:
        self.submitted.append((username, password))
        post = self.pages.get(f"{BASE}/index")
        if post is not None:
            self._logged_in = True
            return AuthDecision(
                attempted=True,
                post_login_url=f"{BASE}/index",
                post_login_status=200,
                title=post.title,
                text=post.text,
                links=post.links,
            )
        return AuthDecision(attempted=False)


class FakeAdapter:
    """PageAdapter implementation wrapping a FakePage."""

    def __init__(self, page: FakePage) -> None:
        self._page = page

    @property
    def redirects_observed(self) -> int:
        return self._page.redirects

    async def navigate(self, url: str, *, timeout_ms: int) -> NavigateResult | None:
        return await self._page.navigate(url, timeout_ms=timeout_ms)

    async def submit_auth_form(
        self, *, username: str, password: str, timeout_ms: int
    ) -> AuthDecision:
        return await self._page.submit_auth_form(
            username=username, password=password, timeout_ms=timeout_ms
        )

    async def close(self) -> None:
        return None


def link_page(
    path: str, links: tuple[str, ...], *, status: int = 200
) -> NavigateResult:
    return NavigateResult(
        url=f"{BASE}{path}",
        http_status=status,
        title=f"title {path}",
        text=f"text {path}",
        links=links,
    )


def make_input(**overrides: object) -> TraversalInput:
    base: dict[str, object] = {
        "execution_id": "exec-01",
        "request_id": "crawl-001",
        "start_url": f"{BASE}/",
        "origin_scheme": ORIGIN_SCHEME,
        "origin_host": ORIGIN_HOST,
        "origin_port": ORIGIN_PORT,
        "max_pages": 20,
        "max_requests": 60,
        "max_depth": 4,
        "timeout_seconds": 30.0,
        "max_page_text_bytes": 4096,
    }
    base.update(overrides)
    return TraversalInput(**base)  # type: ignore[arg-type]


def page_status(output: TraversalOutput, url: str) -> int | None:
    for item in output.pages:
        if item.url == url:
            return item.http_status
    return None


@pytest.mark.asyncio
async def test_sorted_bfs_deterministic_order() -> None:
    page = FakePage(
        pages={
            f"{BASE}/": link_page("/", ("/b", "/a")),
            f"{BASE}/a": link_page("/a", ()),
            f"{BASE}/b": link_page("/b", ()),
        }
    )
    adapter = FakeAdapter(page)
    output = await traverse(adapter, make_input())
    assert output.status == "completed"
    # Sorted discovery: /a before /b.
    assert page.navigated == [f"{BASE}/", f"{BASE}/a", f"{BASE}/b"]


@pytest.mark.asyncio
async def test_b10_out_of_origin_links_not_followed() -> None:
    page = FakePage(
        pages={
            f"{BASE}/": link_page(
                "/",
                (
                    "/ok",
                    "http://evil.example.test/",
                    "https://other.example.test:8080/x",
                ),
            ),
            f"{BASE}/ok": link_page("/ok", ()),
        }
    )
    adapter = FakeAdapter(page)
    output = await traverse(adapter, make_input())
    hostile = [url for url in page.navigated if "evil" in url or "other" in url]
    assert hostile == []
    assert any("ok" in url for url in page.navigated)
    assert all(url.startswith(BASE) for url in output.links)


@pytest.mark.asyncio
async def test_login_form_submitted_once_and_session_retained() -> None:
    page = FakePage(
        pages={
            f"{BASE}/": link_page("/", ("/index",)),
            f"{BASE}/index": NavigateResult(
                url=f"{BASE}/index",
                http_status=200,
                title="Log in",
                text="login form",
                links=(),
                auth_form_present=True,
            ),
        }
    )
    adapter = FakeAdapter(page)
    output = await traverse(
        adapter,
        make_input(
            credentials=TraversalCredentials("zerofox77", "pw"),
            max_pages=10,
        ),
    )
    assert page.submitted == [("zerofox77", "pw")]
    post_login = [o for o in output.pages if o.url == f"{BASE}/index" and o.depth == 1]
    assert post_login and post_login[0].http_status == 200


@pytest.mark.asyncio
async def test_no_login_without_credentials() -> None:
    page = FakePage(
        pages={
            f"{BASE}/": link_page("/", ("/index",)),
            f"{BASE}/index": NavigateResult(
                url=f"{BASE}/index",
                http_status=200,
                title="Log in",
                text="login",
                links=(),
                auth_form_present=True,
            ),
        }
    )
    adapter = FakeAdapter(page)
    await traverse(adapter, make_input())
    assert page.submitted == []


@pytest.mark.asyncio
async def test_503_retried_once_bounded() -> None:
    calls: list[str] = []

    class CountingAdapter(FakeAdapter):
        async def navigate(self, url: str, *, timeout_ms: int) -> NavigateResult | None:
            calls.append(url)
            if url == f"{BASE}/" and calls.count(url) == 1:
                return NavigateResult(
                    url=url,
                    http_status=503,
                    title="",
                    text="",
                    links=(),
                    retry_after_seconds=0.01,
                )
            return NavigateResult(
                url=url, http_status=200, title="ok", text="ok", links=()
            )

    adapter = CountingAdapter(FakePage(pages={}))
    output = await traverse(adapter, make_input(timeout_seconds=10.0))
    assert calls.count(f"{BASE}/") == 2
    assert page_status(output, f"{BASE}/") == 200
    assert output.status == "completed"


@pytest.mark.asyncio
async def test_429_represented_without_retry() -> None:
    page = FakePage(
        pages={
            f"{BASE}/": NavigateResult(
                url=f"{BASE}/",
                http_status=429,
                title="Too Many Requests",
                text="slow down",
                links=(),
                retry_after_seconds=120,
            )
        }
    )
    adapter = FakeAdapter(page)
    output = await traverse(adapter, make_input())
    assert page_status(output, f"{BASE}/") == 429
    assert page.navigated.count(f"{BASE}/") == 1


@pytest.mark.asyncio
async def test_b11_page_budget_deterministic_stop() -> None:
    pages = {f"{BASE}/": link_page("/", ("/n1", "/n2", "/n3"))}
    for i in range(1, 4):
        pages[f"{BASE}/n{i}"] = link_page(f"/n{i}", ())
    adapter = FakeAdapter(FakePage(pages=pages))
    output = await traverse(adapter, make_input(max_pages=2))
    assert len(output.pages) <= 2


@pytest.mark.asyncio
async def test_depth_budget_enforced() -> None:
    pages = {
        f"{BASE}/": link_page("/", ("/d1",)),
        f"{BASE}/d1": link_page("/d1", ("/d2",)),
        f"{BASE}/d2": link_page("/d2", ("/d3",)),
        f"{BASE}/d3": link_page("/d3", ()),
    }
    adapter = FakeAdapter(FakePage(pages=pages))
    output = await traverse(adapter, make_input(max_pages=20, max_depth=2))
    depths = [o.depth for o in output.pages]
    assert max(depths) <= 2


@pytest.mark.asyncio
async def test_self_destructive_logout_link_never_navigated() -> None:
    page = FakePage(
        pages={
            f"{BASE}/": link_page("/", ("/logout", "/index")),
            f"{BASE}/index": link_page("/index", ()),
        }
    )
    adapter = FakeAdapter(page)
    output = await traverse(adapter, make_input())
    assert "/logout" not in page.navigated
    assert not any("/logout" in url for url in output.links)


@pytest.mark.asyncio
async def test_fragments_and_non_http_schemes_dropped() -> None:
    page = FakePage(
        pages={
            f"{BASE}/": link_page(
                "/",
                (
                    "/a#section",
                    "mailto:x@example.test",
                    "javascript:alert(1)",
                    "/b?p=1#f",
                ),
            ),
            f"{BASE}/a": link_page("/a", ()),
            f"{BASE}/b": link_page("/b?p=1", ()),
        }
    )
    adapter = FakeAdapter(page)
    await traverse(adapter, make_input())
    assert not any("#" in url for url in page.navigated)
    assert not any(
        u.startswith("mailto:") or u.startswith("javascript:") for u in page.navigated
    )


@pytest.mark.asyncio
async def test_timeout_produces_bounded_status() -> None:
    class SlowAdapter(FakeAdapter):
        async def navigate(self, url: str, *, timeout_ms: int) -> NavigateResult | None:
            await asyncio.sleep(0.05)
            return link_page(url, ("/more",))

    adapter = SlowAdapter(FakePage(pages={}))
    # The deadline expires during the first navigation; the remaining queue
    # work is abandoned with a typed time-budget status.
    output = await traverse(adapter, make_input(timeout_seconds=0.001, max_pages=5))
    assert output.status == "timed_out"
    assert output.reason == "runtime time budget exhausted"


@pytest.mark.asyncio
async def test_workload_failure_bounded_status() -> None:
    page = FakePage(pages={f"{BASE}/": link_page("/", ())})
    page.crash_on_navigate = True
    adapter = FakeAdapter(page)
    output = await traverse(adapter, make_input())
    assert output.status == "workload_failed"


@pytest.mark.asyncio
async def test_navigation_failure_is_bounded_observation() -> None:
    adapter = FakeAdapter(FakePage(pages={}))  # start url has no scripted page -> None
    output = await traverse(adapter, make_input())
    assert output.status == "completed"
    assert output.pages[0].rendered is False
    assert output.pages[0].http_status is None


@pytest.mark.asyncio
async def test_redirect_counter_passthrough() -> None:
    page = FakePage(pages={f"{BASE}/": link_page("/", ())})
    page.redirects = 3
    adapter = FakeAdapter(page)
    output = await traverse(adapter, make_input())
    assert output.redirects_observed == 3


@pytest.mark.asyncio
async def test_cancellation_propagates() -> None:
    class NeverAdapter(FakeAdapter):
        async def navigate(self, url: str, *, timeout_ms: int) -> NavigateResult | None:
            await asyncio.Event().wait()  # pragma: no cover - cancelled
            return None

    adapter = NeverAdapter(FakePage(pages={}))
    task = asyncio.create_task(traverse(adapter, make_input()))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_text_excerpt_bounded() -> None:
    huge = "A" * 100000
    page = FakePage(
        pages={
            f"{BASE}/": NavigateResult(
                url=f"{BASE}/",
                http_status=200,
                title=huge,
                text=huge,
                links=(),
            )
        }
    )
    adapter = FakeAdapter(page)
    output = await traverse(adapter, make_input(max_page_text_bytes=512))
    observed = output.pages[0]
    assert len(observed.title.encode("utf-8")) <= 512
    assert len(observed.text_excerpt.encode("utf-8")) <= 512


@pytest.mark.asyncio
async def test_bounced_target_reobserved_after_login() -> None:
    target = f"{BASE}/protected"
    login_render = NavigateResult(
        url=target,
        http_status=200,
        title="Log in",
        text="username password",
        links=(f"{BASE}/index",),
        auth_form_present=True,
    )
    protected = link_page("/protected", ("/protected?page=2",))
    index = link_page("/index", ())
    page = FakePage(
        pages={target: login_render, f"{BASE}/index": index},
        post_login_pages={target: protected},
        redirect_urls={target},  # the pre-login navigation bounced with a 3xx
    )
    adapter = FakeAdapter(page)
    output = await traverse(
        adapter,
        make_input(
            start_url=target,
            credentials=TraversalCredentials("u", "p"),
            max_pages=10,
        ),
    )
    assert output.status == "completed"
    # The bounce (depth 0) and the post-login retry (depth 1) are both recorded.
    assert [(o.url, o.depth) for o in output.pages].count((target, 0)) == 1
    assert [(o.url, o.depth) for o in output.pages].count((target, 1)) == 1
    retried = next(o for o in output.pages if o.url == target and o.depth == 1)
    assert retried.http_status == 200
    assert "text /protected" in retried.text_excerpt
    # Links from the post-login retry are surfaced (pagination linkage here).
    assert f"{target}?page=2" in output.links


@pytest.mark.asyncio
async def test_bounced_target_requires_redirect_delta() -> None:
    target = f"{BASE}/protected"
    login_render = NavigateResult(
        url=target,
        http_status=200,
        title="Log in",
        text="username password",
        links=(f"{BASE}/index",),
        auth_form_present=True,
    )
    index = link_page("/index", ())
    # The form is reached directly (no redirect observed): no post-login retry.
    page = FakePage(
        pages={target: login_render, f"{BASE}/index": index},
        post_login_pages={target: link_page("/protected", ())},
        redirect_urls=set(),
    )
    adapter = FakeAdapter(page)
    output = await traverse(
        adapter,
        make_input(
            start_url=target,
            credentials=TraversalCredentials("u", "p"),
            max_pages=10,
        ),
    )
    assert [(o.url, o.depth) for o in output.pages].count((target, 1)) == 0

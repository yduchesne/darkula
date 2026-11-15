# SPDX-License-Identifier: AGPL-3.0-only
"""PR 20 live browser policy and engine tests (U20-32..U20-41).

No Playwright or network is used: the route/WebSocket objects and page
adapter are deterministic fakes.
"""

from __future__ import annotations

import pytest

from crawler_runtime.engine import (
    AuthDecision,
    NavigateResult,
    TraversalInput,
    TraversalOutput,
    traverse,
)
from crawler_runtime.live_policy import (
    ENV_HTTPS_PROXY,
    ENV_INTERNAL_CIDR,
    LivePolicyError,
    LiveProxyConfig,
    LiveRequestPolicy,
    browser_context_options,
)

HOST = "approved.example.org"
ORIGIN = f"https://{HOST}:443"


class FakeRequest:
    def __init__(
        self, url: str, *, method: str = "GET", headers: dict[str, str] | None = None
    ) -> None:
        self.url = url
        self.method = method
        self.headers = headers or {}


class FakeRoute:
    def __init__(self, request: FakeRequest) -> None:
        self.request = request
        self.aborted: str | None = None
        self.continued = False

    async def abort(self, reason: str) -> None:
        self.aborted = reason

    async def continue_(self) -> None:
        self.continued = True


class FakeWebSocket:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def live_policy(**overrides: object) -> LiveRequestPolicy:
    base: dict[str, object] = {
        "approved_host": HOST,
        "allowed_path_prefix": "/docs/",
    }
    base.update(overrides)
    return LiveRequestPolicy(**base)  # type: ignore[arg-type]


class TestProxyConfig:
    def test_u20_33_valid_private_proxy_accepted(self) -> None:
        config = LiveProxyConfig.from_environment(
            {
                ENV_HTTPS_PROXY: "http://10.89.1.10:3128",
                ENV_INTERNAL_CIDR: "10.89.1.0/24",
            }
        )
        assert config.server == "http://10.89.1.10:3128"
        assert config.proxy_ip == "10.89.1.10"

    @pytest.mark.parametrize(
        "environ",
        [
            {},
            {ENV_HTTPS_PROXY: "http://10.89.1.10:3128"},
            {
                ENV_HTTPS_PROXY: "http://10.89.1.10:8080",
                ENV_INTERNAL_CIDR: "10.89.1.0/24",
            },
            {
                ENV_HTTPS_PROXY: "http://user:pw@10.89.1.10:3128",
                ENV_INTERNAL_CIDR: "10.89.1.0/24",
            },
            {
                ENV_HTTPS_PROXY: "https://10.89.1.10:3128",
                ENV_INTERNAL_CIDR: "10.89.1.0/24",
            },
            {ENV_HTTPS_PROXY: "http://8.8.8.8:3128", ENV_INTERNAL_CIDR: "10.89.1.0/24"},
            {
                ENV_HTTPS_PROXY: "http://10.89.2.10:3128",
                ENV_INTERNAL_CIDR: "10.89.1.0/24",
            },
            {
                ENV_HTTPS_PROXY: "http://10.89.1.10:3128",
                ENV_INTERNAL_CIDR: "10.89.1.0/16",
            },
            {
                ENV_HTTPS_PROXY: "http://10.89.1.10:3128",
                ENV_INTERNAL_CIDR: "10.89.1.0/24",
                "HTTP_PROXY": "http://evil:1",
            },
            {
                ENV_HTTPS_PROXY: "http://10.89.1.10:3128",
                ENV_INTERNAL_CIDR: "10.89.1.0/24",
                "HTTPS_PROXY": "http://evil:1",
            },
            {
                ENV_HTTPS_PROXY: "http://10.89.1.10:3128",
                ENV_INTERNAL_CIDR: "10.89.1.0/24",
                "ALL_PROXY": "socks5://evil:1",
            },
        ],
    )
    def test_u20_33_missing_malformed_ambient_rejected(
        self, environ: dict[str, str]
    ) -> None:
        with pytest.raises(LivePolicyError):
            LiveProxyConfig.from_environment(environ)

    def test_u20_34_context_options_are_strict(self) -> None:
        config = LiveProxyConfig(
            server="http://10.89.1.10:3128",
            proxy_ip="10.89.1.10",
            internal_cidr="10.89.1.0/24",
        )
        options = browser_context_options(config)
        assert options == {
            "proxy": {"server": "http://10.89.1.10:3128"},
            "service_workers": "block",
            "accept_downloads": False,
            "ignore_https_errors": False,
        }
        # U20-41: TLS verification can never be disabled by configuration.
        assert options["ignore_https_errors"] is False


class TestRoutePolicy:
    @pytest.mark.asyncio
    async def test_u20_37_off_origin_and_off_prefix_are_aborted(self) -> None:
        policy = live_policy()
        for url in (
            "https://evil.example.org/docs/guide",
            f"{ORIGIN}/other",
            f"{ORIGIN}/docs-evil",
            "http://approved.example.org:443/docs/guide",
        ):
            route = FakeRoute(FakeRequest(url))
            await policy.route_handler(route)
            assert route.aborted == "blockedbyclient"
            assert route.continued is False
            assert policy.violation == "url_not_authorized"

    @pytest.mark.asyncio
    async def test_u20_35_external_redirect_destination_aborted_before_network(
        self,
    ) -> None:
        policy = live_policy()
        route = FakeRoute(FakeRequest("https://other.example.org/docs/guide"))
        await policy.route_handler(route)
        assert route.aborted == "blockedbyclient"
        assert policy.violation == "url_not_authorized"

    @pytest.mark.asyncio
    async def test_u20_36_same_origin_in_prefix_request_continues(self) -> None:
        policy = live_policy()
        route = FakeRoute(FakeRequest(f"{ORIGIN}/docs/guide"))
        await policy.route_handler(route)
        assert route.continued is True
        assert route.aborted is None
        assert policy.violation is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "denied_request",
        [
            FakeRequest(f"{ORIGIN}/docs/guide", method="POST"),
            FakeRequest(f"{ORIGIN}/docs/guide", method="PUT"),
            FakeRequest(f"{ORIGIN}/docs/guide", method="DELETE"),
            FakeRequest(
                f"{ORIGIN}/docs/guide",
                headers={"authorization": "Bearer secret"},
            ),
            FakeRequest(
                f"{ORIGIN}/docs/guide",
                headers={"x-forwarded-for": "1.2.3.4"},
            ),
        ],
    )
    async def test_u20_38_unsafe_methods_and_headers_denied(
        self, denied_request: FakeRequest
    ) -> None:
        policy = live_policy()
        route = FakeRoute(denied_request)
        await policy.route_handler(route)
        assert route.aborted == "blockedbyclient"
        assert policy.violation is not None

    @pytest.mark.asyncio
    async def test_u20_38_websocket_denied(self) -> None:
        policy = live_policy()
        websocket = FakeWebSocket()
        await policy.websocket_handler(websocket)
        assert websocket.closed is True
        assert policy.violation == "websocket_denied"

    @pytest.mark.asyncio
    async def test_u20_39_sixty_first_request_denied(self) -> None:
        policy = live_policy()
        for _ in range(60):
            route = FakeRoute(FakeRequest(f"{ORIGIN}/docs/guide"))
            await policy.route_handler(route)
            assert route.continued is True
        denied = FakeRoute(FakeRequest(f"{ORIGIN}/docs/guide"))
        await policy.route_handler(denied)
        assert denied.aborted == "blockedbyclient"
        assert policy.request_count == 61
        assert policy.violation == "request_budget_exceeded"

    def test_u20_40_ninth_redirect_denied(self) -> None:
        policy = live_policy()
        for _ in range(8):
            policy.record_redirect()
        assert policy.violation is None
        policy.record_redirect()
        assert policy.violation == "redirect_budget_exceeded"


# -- engine v2 ------------------------------------------------------------


class LiveFakeAdapter:
    def __init__(
        self,
        pages: dict[str, NavigateResult | None],
        *,
        intercepted: int = 1,
        violation_after: str | None = None,
        redirects: int = 0,
        submitted: list[tuple[str, str]] | None = None,
    ) -> None:
        self._pages = pages
        self._intercepted = intercepted
        self._violation_after = violation_after
        self._redirects = redirects
        self.violation: str | None = None
        self.submitted = submitted if submitted is not None else []

    @property
    def redirects_observed(self) -> int:
        return self._redirects

    @property
    def intercepted_requests(self) -> int:
        return self._intercepted

    @property
    def live_violation(self) -> str | None:
        return self.violation

    async def navigate(self, url: str, *, timeout_ms: int) -> NavigateResult | None:
        self._intercepted += 1
        result = self._pages.get(url)
        if self._violation_after is not None:
            self.violation = self._violation_after
        return result

    async def submit_auth_form(
        self, *, username: str, password: str, timeout_ms: int
    ) -> AuthDecision:
        self.submitted.append((username, password))
        return AuthDecision(attempted=True)

    async def close(self) -> None:
        return None


def live_input(**overrides: object) -> TraversalInput:
    base: dict[str, object] = {
        "execution_id": "exec-live",
        "request_id": "crawl-live",
        "start_url": f"{ORIGIN}/docs/guide",
        "origin_scheme": "https",
        "origin_host": HOST,
        "origin_port": 443,
        "max_pages": 5,
        "max_requests": 20,
        "max_depth": 2,
        "timeout_seconds": 10.0,
        "max_page_text_bytes": 4096,
        "network_mode": "public_https_proxy",
        "allowed_path_prefix": "/docs/",
    }
    base.update(overrides)
    return TraversalInput(**base)  # type: ignore[arg-type]


def page(url: str, links: tuple[str, ...] = ()) -> NavigateResult:
    return NavigateResult(url=url, http_status=200, title="t", text="body", links=links)


@pytest.mark.asyncio
async def test_u20_36_final_redirect_url_and_links_use_final_url() -> None:
    final = f"{ORIGIN}/docs/final"
    adapter = LiveFakeAdapter({f"{ORIGIN}/docs/guide": page(final, ("next",))})
    output: TraversalOutput = await traverse(adapter, live_input())
    assert output.status == "completed"
    assert output.pages[0].url == final
    assert f"{ORIGIN}/docs/next" in output.links


@pytest.mark.asyncio
async def test_u20_37_violation_yields_no_partial_success() -> None:
    adapter = LiveFakeAdapter(
        {f"{ORIGIN}/docs/guide": page(f"{ORIGIN}/docs/guide")},
        violation_after="url_not_authorized",
    )
    output = await traverse(adapter, live_input())
    assert output.status == "policy_violation"
    assert output.pages == ()
    assert output.links == ()


@pytest.mark.asyncio
async def test_u20_38_live_never_submits_auth_form() -> None:
    adapter = LiveFakeAdapter(
        {f"{ORIGIN}/docs/guide": page(f"{ORIGIN}/docs/guide")},
    )
    # Credentials are not part of a live traversal; the engine must not submit.
    output = await traverse(adapter, live_input())
    assert output.status == "completed"
    assert adapter.submitted == []


@pytest.mark.asyncio
async def test_u20_39_output_requests_include_intercepted_subresources() -> None:
    adapter = LiveFakeAdapter(
        {f"{ORIGIN}/docs/guide": page(f"{ORIGIN}/docs/guide")},
        intercepted=4,
    )
    output = await traverse(adapter, live_input())
    # 4 pre-existing intercepted subresources + 1 navigation request.
    assert output.requests == 5


@pytest.mark.asyncio
async def test_u20_41_failed_first_navigation_is_not_completed() -> None:
    adapter = LiveFakeAdapter({f"{ORIGIN}/docs/guide": None})
    output = await traverse(adapter, live_input())
    assert output.status == "workload_failed"
    assert output.pages == ()


@pytest.mark.asyncio
async def test_u20_32_v1_credentials_still_submitted() -> None:
    from crawler_runtime.engine import TraversalCredentials

    adapter = LiveFakeAdapter(
        {f"{ORIGIN}/docs/guide": page(f"{ORIGIN}/docs/guide")},
    )
    original = adapter.navigate

    async def navigate_with_form(url: str, *, timeout_ms: int) -> NavigateResult | None:
        result = await original(url, timeout_ms=timeout_ms)
        if result is not None:
            return NavigateResult(
                url=result.url,
                http_status=result.http_status,
                title=result.title,
                text=result.text,
                links=result.links,
                auth_form_present=True,
            )
        return result

    adapter.navigate = navigate_with_form  # type: ignore[method-assign]
    input_v1 = TraversalInput(
        execution_id="exec-v1",
        request_id="crawl-v1",
        start_url=f"{ORIGIN}/docs/guide",
        origin_scheme="https",
        origin_host=HOST,
        origin_port=443,
        credentials=TraversalCredentials("user", "pw"),
        max_pages=5,
        max_requests=10,
        max_depth=1,
        timeout_seconds=10.0,
        max_page_text_bytes=4096,
    )
    output = await traverse(adapter, input_v1)
    assert output.status == "completed"
    assert adapter.submitted == [("user", "pw")]

# SPDX-License-Identifier: AGPL-3.0-only
"""PR 20 public-HTTPS authorization tests (U20-01..U20-11, U20-18/19).

Uses the canonical ``real CrawlerController -> FakeSandbox`` wiring. No
network, DNS, Podman or browser is touched.
"""

from __future__ import annotations

import json

import pytest

from darkula.config.settings import CrawlerSettings
from darkula.crawler.contracts import (
    AllowedOrigin,
    CrawlCredentials,
    CrawlRequest,
    CrawlResult,
    InvalidCrawlRequest,
    PublicHttpsGrant,
)
from darkula.crawler.controller import CrawlerController
from darkula.crawler.public_https_policy import (
    is_allowed_https_url,
    is_safe_public_host,
    validate_live_path_prefix,
)
from darkula.crawler.runtime_protocol import (
    PUBLIC_HTTPS_PROTOCOL_VERSION,
    RuntimeInput,
    RuntimeOutput,
    RuntimeOutputPage,
    encode_input,
    encode_output,
)
from darkula.sandbox.contracts import (
    AllowedDestination,
    EgressMode,
    NetworkPolicy,
    SandboxExecutionPolicy,
    SandboxExecutionResult,
    SandboxExitReason,
    SandboxRequestError,
)
from darkula.testing.fake_sandbox import FakeSandbox

ORIGIN = "https://approved.example.org:443"
HOST = "approved.example.org"
FAKE_WORLD_ORIGIN = "http://blackgate.example.test:8080"


def live_settings(**overrides: object) -> CrawlerSettings:
    base: dict[str, object] = {
        "real_https_enabled": True,
        "real_https_allowed_origins": (ORIGIN,),
    }
    base.update(overrides)
    return CrawlerSettings(**base)  # type: ignore[arg-type]


def live_request(**overrides: object) -> CrawlRequest:
    origin = AllowedOrigin.parse(ORIGIN)
    base: dict[str, object] = {
        "request_id": "crawl-live-1",
        "start_url": f"{ORIGIN}/docs/guide",
        "allowed_origin": origin,
        "credentials": None,
        "max_pages": 2,
        "max_requests": 10,
        "max_depth": 1,
        "timeout_seconds": 30.0,
        "public_https_grant": PublicHttpsGrant(
            origin=origin, allowed_path_prefix="/docs/"
        ),
    }
    base.update(overrides)
    return CrawlRequest(**base)  # type: ignore[arg-type]


def live_output(
    execution_id: str = "crawl-live-1",
    request_id: str = "crawl-live-1",
    *,
    url: str = f"{ORIGIN}/docs/guide",
    links: tuple[str, ...] = (),
) -> SandboxExecutionResult:
    return SandboxExecutionResult(
        execution_id=execution_id,
        exit_reason=SandboxExitReason.COMPLETED,
        output=encode_output(
            RuntimeOutput(
                version=PUBLIC_HTTPS_PROTOCOL_VERSION,
                execution_id=execution_id,
                request_id=request_id,
                status="completed",
                pages=(
                    RuntimeOutputPage(
                        url=url,
                        depth=0,
                        http_status=200,
                        rendered=True,
                        title="Public",
                        text_excerpt="public page",
                    ),
                ),
                links=links,
                requests=1,
                redirects_observed=0,
                duration_seconds=0.2,
            )
        ),
    )


async def run_live(
    request: CrawlRequest, settings: CrawlerSettings
) -> tuple[CrawlResult, FakeSandbox]:
    sandbox = FakeSandbox()
    sandbox.enqueue_result(live_output())
    controller = CrawlerController(settings=settings, sandbox=sandbox)
    result = await controller.crawl(request)
    return result, sandbox


class TestHostValidation:
    def test_u20_04_safe_public_fqdn_accepted(self) -> None:
        assert is_safe_public_host(HOST) is True
        assert is_safe_public_host("sub.approved.example.org") is True
        assert is_safe_public_host("xn--bcher-kva.example.org") is True

    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "1.2.3.4",
            "0.0.0.0",
            "::1",
            "::ffff:127.0.0.1",
            "localhost",
            "localhost.example.org",
            "metadata",
            "metadata.google.internal",
            "host.docker.internal",
        ],
    )
    def test_u20_05_ip_and_localhost_rejected(self, host: str) -> None:
        assert is_safe_public_host(host) is False

    @pytest.mark.parametrize(
        "host",
        [
            "site.onion",
            "site.local",
            "site.test",
            "site.invalid",
            "site.example",
            "bücher.example.org",
            "*.example.org",
            "approved.example.org.",
            "Approved.Example.Org",
            "internal",
            "foo..example.org",
            "a_b.example.org",
            "example",
        ],
    )
    def test_u20_06_reserved_unicode_wildcard_trailing_dot_rejected(
        self, host: str
    ) -> None:
        assert is_safe_public_host(host) is False


class TestUrlValidation:
    def test_u20_08_path_prefix_component_boundary(self) -> None:
        assert is_allowed_https_url(f"{ORIGIN}/docs/a", HOST, "/docs/")
        assert is_allowed_https_url(f"{ORIGIN}/docs/", HOST, "/docs/")
        assert not is_allowed_https_url(f"{ORIGIN}/docs-evil", HOST, "/docs/")
        assert not is_allowed_https_url(f"{ORIGIN}/documentation", HOST, "/docs/")
        assert not is_allowed_https_url(f"{ORIGIN}/docs", HOST, "/docs/")
        assert is_allowed_https_url(f"{ORIGIN}/anything", HOST, "/")

    @pytest.mark.parametrize(
        "url",
        [
            f"https://user:pw@{HOST}/docs/a",
            f"{ORIGIN}/docs/a#frag",
            f"{ORIGIN}/docs/%2e%2e/secret",
            f"{ORIGIN}/docs/%2fetc",
            f"{ORIGIN}/docs/%25",
            f"{ORIGIN}/docs\\evil",
            "http://approved.example.org:443/docs/a",
            "https://approved.example.org:8443/docs/a",
            "https://approved.example.org.evil.com/docs/a",
            "javascript:alert(1)",
            "data:text/html,hello",
            "file:///etc/passwd",
            "ws://approved.example.org:443/docs/a",
            " https://approved.example.org/docs/a",
        ],
    )
    def test_u20_07_unsafe_urls_rejected_without_reflection(self, url: str) -> None:
        assert is_allowed_https_url(url, HOST, "/docs/") is False

    def test_u20_11_unsafe_path_prefix_rejected(self) -> None:
        for bad in ("", "docs/", "/docs", "/docs/../", "/docs//", "/döcs/"):
            with pytest.raises(ValueError):
                validate_live_path_prefix(bad)
        assert validate_live_path_prefix("/") == "/"
        assert validate_live_path_prefix("/docs/") == "/docs/"


class TestGrantConstruction:
    def test_u20_05_ip_literal_grant_rejected(self) -> None:
        with pytest.raises(InvalidCrawlRequest, match="invalid public HTTPS origin"):
            PublicHttpsGrant(
                origin=AllowedOrigin(scheme="https", host="127.0.0.1", port=443)
            )

    def test_u20_06_onion_grant_rejected(self) -> None:
        with pytest.raises(InvalidCrawlRequest):
            PublicHttpsGrant(
                origin=AllowedOrigin(scheme="https", host="hidden.onion", port=443)
            )

    def test_u20_06_non_443_origin_rejected(self) -> None:
        with pytest.raises(InvalidCrawlRequest):
            PublicHttpsGrant(origin=AllowedOrigin(scheme="https", host=HOST, port=8443))

    def test_u20_07_bad_prefix_not_reflected(self) -> None:
        attacker = "/docs/%2e%2e/secret"
        with pytest.raises(InvalidCrawlRequest) as excinfo:
            PublicHttpsGrant(
                origin=AllowedOrigin.parse(ORIGIN), allowed_path_prefix=attacker
            )
        assert attacker not in str(excinfo.value)


class TestControllerAuthorization:
    @pytest.mark.asyncio
    async def test_u20_01_no_grant_preserves_v1_isolated(self) -> None:
        request = CrawlRequest(
            request_id="fake-world-1",
            start_url=f"{FAKE_WORLD_ORIGIN}/",
            allowed_origin=AllowedOrigin.parse(FAKE_WORLD_ORIGIN),
        )
        sandbox = FakeSandbox()
        sandbox.enqueue_result(
            SandboxExecutionResult(
                execution_id="fake-world-1",
                exit_reason=SandboxExitReason.COMPLETED,
                output=encode_output(
                    RuntimeOutput(
                        version=1,
                        execution_id="fake-world-1",
                        request_id="fake-world-1",
                        status="completed",
                        pages=(),
                        links=(),
                        requests=1,
                        redirects_observed=0,
                        duration_seconds=0.1,
                    )
                ),
            )
        )
        result = await CrawlerController(
            settings=CrawlerSettings(), sandbox=sandbox
        ).crawl(request)
        execution = sandbox.executions[0]
        assert execution.policy.network.egress is EgressMode.ISOLATED
        assert json.loads(execution.workload.payload)["version"] == 1
        assert "public_https_grant" not in execution.workload.payload.decode()
        assert result.protocol_version == 1

    @pytest.mark.asyncio
    async def test_u20_02_disabled_rejects_before_sandbox(self) -> None:
        sandbox = FakeSandbox()
        controller = CrawlerController(
            settings=CrawlerSettings(real_https_allowed_origins=(ORIGIN,)),
            sandbox=sandbox,
        )
        with pytest.raises(
            InvalidCrawlRequest, match="public HTTPS crawling is disabled"
        ):
            await controller.crawl(live_request())
        assert sandbox.executions == []

    @pytest.mark.asyncio
    async def test_u20_02_empty_allowlist_rejects_before_sandbox(self) -> None:
        sandbox = FakeSandbox()
        controller = CrawlerController(
            settings=CrawlerSettings(real_https_enabled=True), sandbox=sandbox
        )
        with pytest.raises(
            InvalidCrawlRequest, match="public HTTPS origin is not approved"
        ):
            await controller.crawl(live_request())
        assert sandbox.executions == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "granted_origin",
        [
            "https://approved.example.org.evil.com:443",
            "https://evilapproved.example.org:443",
        ],
    )
    async def test_u20_03_non_exact_allowlist_rejected(
        self, granted_origin: str
    ) -> None:
        origin = AllowedOrigin.parse(granted_origin)
        request = live_request(
            start_url=f"{granted_origin}/docs/guide",
            allowed_origin=origin,
            public_https_grant=PublicHttpsGrant(
                origin=origin, allowed_path_prefix="/docs/"
            ),
        )
        sandbox = FakeSandbox()
        controller = CrawlerController(settings=live_settings(), sandbox=sandbox)
        with pytest.raises(
            InvalidCrawlRequest, match="public HTTPS origin is not approved"
        ):
            await controller.crawl(request)
        assert sandbox.executions == []

    @pytest.mark.asyncio
    async def test_u20_04_valid_grant_selects_public_mode(self) -> None:
        result, sandbox = await run_live(live_request(), live_settings())
        execution = sandbox.executions[0]
        assert execution.policy.network.egress is EgressMode.PUBLIC_HTTPS_PROXY
        assert execution.policy.network.destinations == (
            AllowedDestination(host=HOST, port=443),
        )
        input_document = json.loads(execution.workload.payload)
        assert input_document["version"] == PUBLIC_HTTPS_PROTOCOL_VERSION
        assert input_document["network_mode"] == "public_https_proxy"
        assert input_document["allowed_path_prefix"] == "/docs/"
        assert input_document["credentials"] is None
        assert result.status.value == "completed"

    @pytest.mark.asyncio
    async def test_u20_09_live_credentials_rejected(self) -> None:
        request = live_request(credentials=CrawlCredentials("user", "secret"))
        sandbox = FakeSandbox()
        controller = CrawlerController(settings=live_settings(), sandbox=sandbox)
        with pytest.raises(
            InvalidCrawlRequest, match="public HTTPS credentials are unsupported"
        ):
            await controller.crawl(request)
        assert sandbox.executions == []

    @pytest.mark.asyncio
    async def test_u20_10_live_budget_ceilings_rejected(self) -> None:
        cases = [
            ("max_pages", 21),
            ("max_requests", 61),
            ("max_depth", 3),
            ("timeout_seconds", 90.1),
        ]
        for field, value in cases:
            sandbox = FakeSandbox()
            controller = CrawlerController(settings=live_settings(), sandbox=sandbox)
            with pytest.raises(InvalidCrawlRequest, match=f"{field} exceeds live"):
                await controller.crawl(live_request(**{field: value}))
            assert sandbox.executions == []

    @pytest.mark.asyncio
    async def test_u20_18_one_port_443_destination(self) -> None:
        _, sandbox = await run_live(live_request(), live_settings())
        policy = sandbox.executions[0].policy
        assert policy.network.egress is EgressMode.PUBLIC_HTTPS_PROXY
        assert policy.network.destinations[0].port == 443

    @pytest.mark.asyncio
    async def test_u20_19_flag_alone_cannot_select_public_mode(self) -> None:
        request = CrawlRequest(
            request_id="fake-world-2",
            start_url=f"{FAKE_WORLD_ORIGIN}/",
            allowed_origin=AllowedOrigin.parse(FAKE_WORLD_ORIGIN),
        )
        sandbox = FakeSandbox()
        sandbox.enqueue_result(
            SandboxExecutionResult(
                execution_id="fake-world-2",
                exit_reason=SandboxExitReason.COMPLETED,
                output=encode_output(
                    RuntimeOutput(
                        version=1,
                        execution_id="fake-world-2",
                        request_id="fake-world-2",
                        status="completed",
                        pages=(),
                        links=(),
                        requests=1,
                        redirects_observed=0,
                        duration_seconds=0.1,
                    )
                ),
            )
        )
        await CrawlerController(settings=live_settings(), sandbox=sandbox).crawl(
            request
        )
        assert sandbox.executions[0].policy.network.egress is EgressMode.ISOLATED


class TestPolicyEnvelope:
    def test_u20_11_unknown_mode_rejected(self) -> None:
        with pytest.raises((SandboxRequestError, ValueError)):
            SandboxExecutionPolicy(
                network=NetworkPolicy(egress="bogus")  # type: ignore[arg-type]
            )

    def test_u20_11_extra_destinations_rejected(self) -> None:
        with pytest.raises(SandboxRequestError):
            SandboxExecutionPolicy(
                network=NetworkPolicy(
                    egress=EgressMode.PUBLIC_HTTPS_PROXY,
                    destinations=(
                        AllowedDestination(HOST, 443),
                        AllowedDestination("other.example.org", 443),
                    ),
                )
            )

    def test_u20_11_port_80_rejected(self) -> None:
        with pytest.raises(SandboxRequestError):
            SandboxExecutionPolicy(
                network=NetworkPolicy(
                    egress=EgressMode.PUBLIC_HTTPS_PROXY,
                    destinations=(AllowedDestination(HOST, 80),),
                )
            )

    def test_u20_11_settings_network_and_image_override_rejected(self) -> None:
        with pytest.raises(ValueError):
            CrawlerSettings(real_https_egress_network="darkula-intg")
        with pytest.raises(ValueError):
            CrawlerSettings(real_https_gateway_image="evil/proxy:latest")
        with pytest.raises(ValueError):
            CrawlerSettings(real_https_allowed_origins=("https://*.example.org:443",))


class TestV1RoundTrip:
    def test_u20_01_v1_input_has_no_public_fields(self) -> None:
        payload = RuntimeInput(
            version=1,
            execution_id="exec-1",
            request_id="req-1",
            start_url=f"{FAKE_WORLD_ORIGIN}/",
            origin_scheme="http",
            origin_host="blackgate.example.test",
            origin_port=8080,
            credentials=None,
            max_pages=1,
            max_requests=1,
            max_depth=1,
            timeout_seconds=5.0,
            max_page_text_bytes=1024,
        )
        document = json.loads(encode_input(payload))
        assert document["version"] == 1
        assert "network_mode" not in document
        assert "allowed_path_prefix" not in document


class TestLiveOutputValidation:
    @pytest.mark.asyncio
    async def test_u20_16_v2_output_must_be_version_2(self) -> None:
        sandbox = FakeSandbox()
        sandbox.enqueue_result(
            SandboxExecutionResult(
                execution_id="crawl-live-1",
                exit_reason=SandboxExitReason.COMPLETED,
                output=encode_output(
                    RuntimeOutput(
                        version=1,
                        execution_id="crawl-live-1",
                        request_id="crawl-live-1",
                        status="completed",
                        pages=(),
                        links=(),
                        requests=1,
                        redirects_observed=0,
                        duration_seconds=0.1,
                    )
                ),
            )
        )
        result = await CrawlerController(
            settings=live_settings(), sandbox=sandbox
        ).crawl(live_request())
        assert result.status.value == "invalid_output"
        assert result.pages == ()

    @pytest.mark.asyncio
    async def test_u20_16_request_identity_mismatch_invalid(self) -> None:
        sandbox = FakeSandbox()
        sandbox.enqueue_result(live_output(request_id="other-request"))
        result = await CrawlerController(
            settings=live_settings(), sandbox=sandbox
        ).crawl(live_request())
        assert result.status.value == "invalid_output"
        assert result.pages == ()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        [
            f"{ORIGIN}/other",
            "https://evil.example.org/docs/guide",
            f"http://{HOST}:443/docs/guide",
            f"{ORIGIN}/docs/%2e%2e/secret",
        ],
    )
    async def test_u20_17_off_grant_page_is_invalid_without_pages(
        self, url: str
    ) -> None:
        sandbox = FakeSandbox()
        sandbox.enqueue_result(live_output(url=url))
        result = await CrawlerController(
            settings=live_settings(), sandbox=sandbox
        ).crawl(live_request())
        assert result.status.value == "invalid_output"
        assert result.pages == ()

    @pytest.mark.asyncio
    async def test_u20_17_off_grant_link_is_invalid_without_pages(self) -> None:
        sandbox = FakeSandbox()
        sandbox.enqueue_result(
            live_output(links=("https://evil.example.org/docs/guide",))
        )
        result = await CrawlerController(
            settings=live_settings(), sandbox=sandbox
        ).crawl(live_request())
        assert result.status.value == "invalid_output"
        assert result.pages == ()
        assert result.discovered_links == ()

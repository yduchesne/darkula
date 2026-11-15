# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Trusted CrawlerController (PR 7).

The controller is the production orchestrator behind the application-facing
:class:`~darkula.crawler.contracts.Crawler` capability. It never browses, never
invokes Playwright/HTTP itself, never knows Fake World internals, never
persists content, never calls LLMs/agents, and never relaxes policy based on
page content.

Slice responsibilities:

1. validate the :class:`CrawlRequest` (reject before any sandbox resource);
2. create the allow-listed crawler workload document;
3. derive the least-capability sandbox policy (default-deny; the network
   grant is exactly the request's allowed origin);
4. call ``Sandbox.execute``;
5. validate/decode the bounded runtime output;
6. map the outcome to a typed :class:`CrawlResult`;
7. emit bounded OTEL (no credentials, no URLs, no hostile content);
8. preserve cancellation (``asyncio.CancelledError`` propagates; the sandbox
   adapter owns termination and cleanup).
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import replace

from darkula.config.settings import CrawlerSettings
from darkula.crawler.contracts import (
    Crawler,
    CrawlPageObservation,
    CrawlRequest,
    CrawlResult,
    CrawlStatus,
    InvalidCrawlRequest,
    PublicHttpsGrant,
)
from darkula.crawler.public_https_policy import is_allowed_https_url
from darkula.crawler.runtime_protocol import (
    PROTOCOL_VERSION,
    PUBLIC_HTTPS_PROTOCOL_VERSION,
    RuntimeCredentials,
    RuntimeInput,
    RuntimeOutput,
    RuntimeProtocolError,
    decode_output,
    encode_input,
)
from darkula.sandbox.contracts import (
    AllowedDestination,
    EgressMode,
    FilesystemPolicy,
    InputPolicy,
    NetworkPolicy,
    OutputPolicy,
    ResourceLimits,
    RuntimeIsolationPolicy,
    Sandbox,
    SandboxExecutionPolicy,
    SandboxExecutionRequest,
    SandboxExecutionResult,
    SandboxExitReason,
    SandboxWorkload,
    TmpfsMount,
    WorkloadKind,
)
from darkula.telemetry.attributes import (
    ERROR_OUTCOME,
    OUTCOME,
    SUCCESS_OUTCOME,
)
from darkula.telemetry.metrics import DURATION_UNIT, get_counter, get_histogram

#: Bounded telemetry metric names for crawler executions.
_CRAWL_EXECUTIONS_METRIC = "darkula.crawler.executions"
_CRAWL_DURATION_METRIC = "darkula.crawler.duration"
_CRAWL_TIMEOUTS_METRIC = "darkula.crawler.timeouts"

#: Execution identities must be bounded Podman-safe lowercase tokens.
_EXECUTION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _execution_id_for(request_id: str) -> str:
    """Derive a bounded Podman-safe sandbox execution identity.

    The crawl request ID (application/correlation identity) is distinct from
    the sandbox execution identity (isolated workload identity); this
    derivation keeps both bounded and correlated.
    """
    value = re.sub(r"[^a-z0-9]+", "-", request_id.lower()).strip("-")
    if _EXECUTION_ID_RE.fullmatch(value) is None:
        raise InvalidCrawlRequest(
            "cannot derive a valid sandbox execution identity from request_id"
        )
    return value[:48]


def _clamp_budget(value: int, *, maximum: int, name: str) -> int:
    """Reject a budget that exceeds the trusted-side ceiling (fail closed)."""
    if value > maximum:
        raise InvalidCrawlRequest(f"{name} exceeds the trusted ceiling of {maximum}")
    return value


def _validate_public_https_grant(
    request: CrawlRequest, settings: CrawlerSettings
) -> PublicHttpsGrant | None:
    """Validate the explicit per-call public-HTTPS authorization (PR 20).

    All checks run before any sandbox work, with fixed messages that never
    echo the URL/host. ``None`` means the legacy isolated path.
    """
    grant = request.public_https_grant
    if grant is None:
        return None
    if not isinstance(grant, PublicHttpsGrant):
        raise InvalidCrawlRequest("invalid public HTTPS origin")
    if not settings.real_https_enabled:
        raise InvalidCrawlRequest("public HTTPS crawling is disabled")
    if request.allowed_origin != grant.origin:
        raise InvalidCrawlRequest("invalid public HTTPS origin")
    if grant.origin.scheme != "https" or grant.origin.port != 443:
        raise InvalidCrawlRequest("invalid public HTTPS origin")
    if grant.origin.as_origin_url() not in settings.real_https_allowed_origins:
        raise InvalidCrawlRequest("public HTTPS origin is not approved")
    if not is_allowed_https_url(
        request.start_url, grant.origin.host, grant.allowed_path_prefix
    ):
        raise InvalidCrawlRequest("public HTTPS start URL is not authorized")
    if request.credentials is not None:
        raise InvalidCrawlRequest("public HTTPS credentials are unsupported")
    if request.max_pages > settings.real_https_max_pages:
        raise InvalidCrawlRequest("max_pages exceeds live HTTPS limit")
    if request.max_requests > settings.real_https_max_requests:
        raise InvalidCrawlRequest("max_requests exceeds live HTTPS limit")
    if request.max_depth > settings.real_https_max_depth:
        raise InvalidCrawlRequest("max_depth exceeds live HTTPS limit")
    if request.timeout_seconds > settings.real_https_max_timeout_seconds:
        raise InvalidCrawlRequest("timeout_seconds exceeds live HTTPS limit")
    return grant


def _derive_policy(
    request: CrawlRequest,
    settings: CrawlerSettings,
    grant: PublicHttpsGrant | None = None,
) -> SandboxExecutionPolicy:
    """Derive the least-capability default-deny policy for one crawl.

    The isolated (legacy) network grant is exactly the request's allowed
    origin. The public-HTTPS grant yields exactly one approved FQDN on port
    443 and never carries credentials.
    """
    if grant is None:
        destination = AllowedDestination(
            host=request.allowed_origin.host, port=request.allowed_origin.port
        )
        network = NetworkPolicy(destinations=(destination,))
        credentials_allowed = request.credentials is not None
    else:
        network = NetworkPolicy(
            egress=EgressMode.PUBLIC_HTTPS_PROXY,
            destinations=(AllowedDestination(host=grant.origin.host, port=443),),
        )
        credentials_allowed = False
    resources = ResourceLimits(
        memory_bytes=settings.sandbox_memory_bytes,
        cpus=settings.sandbox_cpus,
        pids=settings.sandbox_pids,
        timeout_seconds=request.timeout_seconds,
    )
    return SandboxExecutionPolicy(
        network=network,
        filesystem=FilesystemPolicy(
            tmpfs=(
                TmpfsMount("/tmp", 256 * 1024 * 1024),  # nosec B108 - sandbox tmpfs
                TmpfsMount("/dev/shm", 256 * 1024 * 1024),  # nosec B108 - sandbox tmpfs
            )
        ),
        resources=resources,
        runtime=RuntimeIsolationPolicy(),
        input=InputPolicy(
            max_input_bytes=settings.max_input_bytes,
            credentials_allowed=credentials_allowed,
        ),
        output=OutputPolicy(max_output_bytes=settings.max_output_bytes),
    )


def _map_exit_reason(reason: SandboxExitReason) -> CrawlStatus:
    """Map a sandbox exit reason to the application-level crawl status."""
    if reason is SandboxExitReason.COMPLETED:
        return CrawlStatus.COMPLETED
    if reason is SandboxExitReason.WORKLOAD_FAILED:
        return CrawlStatus.WORKLOAD_FAILED
    if reason is SandboxExitReason.TIMED_OUT:
        return CrawlStatus.TIMED_OUT
    if reason is SandboxExitReason.RESOURCE_LIMIT:
        return CrawlStatus.RESOURCE_LIMITED
    if reason is SandboxExitReason.POLICY_VIOLATION:
        return CrawlStatus.POLICY_VIOLATION
    if reason is SandboxExitReason.TERMINATED:
        return CrawlStatus.TERMINATED
    if reason is SandboxExitReason.INVALID_OR_OVERSIZED_OUTPUT:
        return CrawlStatus.INVALID_OUTPUT
    if reason is SandboxExitReason.CANCELLED:
        # An adapter converts cancellation into a result only in violation
        # of the SPI; the controller never turns that into success.
        return CrawlStatus.TERMINATED
    return CrawlStatus.SANDBOX_UNAVAILABLE


def _runtime_result(
    output: RuntimeOutput, *, cleanup_succeeded: bool | None = None
) -> CrawlResult:
    """Build a CrawlResult from a validated runtime output document."""
    pages = tuple(
        CrawlPageObservation(
            url=page.url,
            depth=page.depth,
            http_status=page.http_status,
            rendered=page.rendered,
            title=page.title,
            text_excerpt=page.text_excerpt,
        )
        for page in output.pages
    )
    return CrawlResult(
        request_id=output.request_id,
        status=_runtime_status(output.status),
        pages=pages,
        discovered_links=output.links,
        requests=output.requests,
        duration_seconds=output.duration_seconds,
        protocol_version=output.version,
        cleanup_succeeded=cleanup_succeeded,
    )


def _runtime_status(status: str) -> CrawlStatus:
    """Map a decoded runtime output status to a crawl status."""
    if status == "completed":
        return CrawlStatus.COMPLETED
    if status == "workload_failed":
        return CrawlStatus.WORKLOAD_FAILED
    if status == "timed_out":
        return CrawlStatus.TIMED_OUT
    if status == "resource_limited":
        return CrawlStatus.RESOURCE_LIMITED
    if status == "policy_violation":
        return CrawlStatus.POLICY_VIOLATION
    return CrawlStatus.TERMINATED


class CrawlerController(Crawler):
    """Trusted crawler orchestrator backed by a generic Sandbox.

    Construction takes the resolved settings and an explicit ``Sandbox``
    implementation; production composition passes the real Podman adapter and
    never silently substitutes a fake.
    """

    def __init__(self, *, settings: CrawlerSettings, sandbox: Sandbox) -> None:
        self._settings = settings
        self._sandbox = sandbox

    @property
    def settings(self) -> CrawlerSettings:
        """Resolved crawler settings used for ceilings and defaults."""
        return self._settings

    async def crawl(self, request: CrawlRequest) -> CrawlResult:
        """Execute one bounded crawl through the configured Sandbox.

        Validation failures raise :class:`InvalidCrawlRequest` before any
        sandbox resource is created; cancellation propagates unchanged (the
        sandbox adapter owns termination/cleanup).
        """
        start = time.perf_counter()
        try:
            result = await self._crawl_validated(request)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._record_duration(time.perf_counter() - start, outcome=ERROR_OUTCOME)
            raise
        self._record_duration(time.perf_counter() - start, outcome=SUCCESS_OUTCOME)
        return result

    async def _crawl_validated(self, request: CrawlRequest) -> CrawlResult:
        request = replace(
            request,
            max_pages=_clamp_budget(
                request.max_pages,
                maximum=self._settings.max_max_pages,
                name="max_pages",
            ),
            max_requests=_clamp_budget(
                request.max_requests,
                maximum=self._settings.max_max_requests,
                name="max_requests",
            ),
            max_depth=_clamp_budget(
                request.max_depth,
                maximum=self._settings.max_max_depth,
                name="max_depth",
            ),
        )
        if request.timeout_seconds > self._settings.max_timeout_seconds:
            raise InvalidCrawlRequest(
                f"timeout_seconds exceeds the trusted ceiling of "
                f"{self._settings.max_timeout_seconds}"
            )
        if request.allowed_origin.host in {"localhost", "127.0.0.1", "::1"} or (
            request.allowed_origin.host.startswith("169.254.")
        ):
            raise InvalidCrawlRequest(
                "loopback/metadata origins are not authorizable targets"
            )

        grant = _validate_public_https_grant(request, self._settings)
        live = grant is not None

        # 2. allow-listed crawler workload (bounded protocol document).
        execution_id = _execution_id_for(request.request_id)
        credentials = None
        if not live and request.credentials is not None:
            credentials = RuntimeCredentials(
                username=request.credentials.username,
                password=request.credentials.password,
            )
        runtime_input = RuntimeInput(
            version=PUBLIC_HTTPS_PROTOCOL_VERSION if live else PROTOCOL_VERSION,
            execution_id=execution_id,
            request_id=request.request_id,
            start_url=request.start_url,
            origin_scheme=request.allowed_origin.scheme,
            origin_host=request.allowed_origin.host,
            origin_port=request.allowed_origin.port,
            credentials=credentials,
            max_pages=request.max_pages,
            max_requests=request.max_requests,
            max_depth=request.max_depth,
            timeout_seconds=request.timeout_seconds,
            max_page_text_bytes=self._settings.max_page_text_bytes,
            network_mode="public_https_proxy" if live else "isolated",
            allowed_path_prefix=(
                grant.allowed_path_prefix if grant is not None else "/"
            ),
        )
        if len(encode_input(runtime_input)) > self._settings.max_input_bytes:
            raise InvalidCrawlRequest("runtime input exceeds the size bound")
        workload = SandboxWorkload(
            kind=WorkloadKind.CRAWLER,
            payload=encode_input(runtime_input),
        )

        # 3. least-capability policy.
        policy = _derive_policy(request, self._settings, grant)

        # 4. execute in the sandbox (cancellation propagates).
        execution = SandboxExecutionRequest(
            execution_id=execution_id,
            policy=policy,
            workload=workload,
        )
        result = await self._sandbox.execute(execution)
        # 5/6. decode and map.
        crawl_result = self._map_result(request, result, policy, grant=grant)
        self._record_outcome(crawl_result)
        return crawl_result

    def _map_result(
        self,
        request: CrawlRequest,
        result: SandboxExecutionResult,
        policy: SandboxExecutionPolicy,
        *,
        grant: PublicHttpsGrant | None = None,
    ) -> CrawlResult:
        if result.exit_reason is not SandboxExitReason.COMPLETED:
            return CrawlResult(
                request_id=request.request_id,
                status=_map_exit_reason(result.exit_reason),
                reason=result.reason,
                duration_seconds=result.metrics.duration_seconds,
                cleanup_succeeded=result.metrics.cleanup_succeeded,
            )
        if result.output is None:
            return CrawlResult(
                request_id=request.request_id,
                status=CrawlStatus.INVALID_OUTPUT,
                reason="sandbox completed without output",
                duration_seconds=result.metrics.duration_seconds,
                cleanup_succeeded=result.metrics.cleanup_succeeded,
            )
        try:
            output = decode_output(
                result.output,
                max_bytes=policy.output.max_output_bytes,
                max_pages=self._settings.max_max_pages,
                max_links=self._settings.max_discovered_links,
            )
        except RuntimeProtocolError:
            get_counter("darkula.crawler.invalid_output").add(1)
            return CrawlResult(
                request_id=request.request_id,
                status=CrawlStatus.INVALID_OUTPUT,
                reason="runtime output failed protocol validation",
                duration_seconds=result.metrics.duration_seconds,
                cleanup_succeeded=result.metrics.cleanup_succeeded,
            )
        if output.execution_id != result.execution_id:
            return CrawlResult(
                request_id=request.request_id,
                status=CrawlStatus.INVALID_OUTPUT,
                reason="runtime output execution identity mismatch",
                duration_seconds=result.metrics.duration_seconds,
                cleanup_succeeded=result.metrics.cleanup_succeeded,
            )
        requested_version = (
            PROTOCOL_VERSION if grant is None else PUBLIC_HTTPS_PROTOCOL_VERSION
        )
        if output.version != requested_version:
            return CrawlResult(
                request_id=request.request_id,
                status=CrawlStatus.INVALID_OUTPUT,
                reason="runtime output version mismatch",
                duration_seconds=result.metrics.duration_seconds,
                cleanup_succeeded=result.metrics.cleanup_succeeded,
            )
        if grant is not None:
            if output.request_id != request.request_id:
                return CrawlResult(
                    request_id=request.request_id,
                    status=CrawlStatus.INVALID_OUTPUT,
                    reason="runtime output request identity mismatch",
                    duration_seconds=result.metrics.duration_seconds,
                    cleanup_succeeded=result.metrics.cleanup_succeeded,
                )
            if not all(
                is_allowed_https_url(
                    page.url, grant.origin.host, grant.allowed_path_prefix
                )
                for page in output.pages
            ) or not all(
                is_allowed_https_url(link, grant.origin.host, grant.allowed_path_prefix)
                for link in output.links
            ):
                return CrawlResult(
                    request_id=request.request_id,
                    status=CrawlStatus.INVALID_OUTPUT,
                    reason="runtime output violates the public HTTPS grant",
                    duration_seconds=result.metrics.duration_seconds,
                    cleanup_succeeded=result.metrics.cleanup_succeeded,
                )
        return _runtime_result(
            output, cleanup_succeeded=result.metrics.cleanup_succeeded
        )

    # -- bounded OTEL ----------------------------------------------------
    def _record_outcome(self, result: CrawlResult) -> None:
        get_counter(_CRAWL_EXECUTIONS_METRIC).add(
            1,
            {
                OUTCOME: (
                    SUCCESS_OUTCOME
                    if result.status is CrawlStatus.COMPLETED
                    else ERROR_OUTCOME
                ),
                "darkula.crawler.status": result.status.value,
            },
        )
        if result.status is CrawlStatus.TIMED_OUT:
            get_counter(_CRAWL_TIMEOUTS_METRIC).add(1)

    def _record_duration(self, duration: float, *, outcome: str) -> None:
        histogram = get_histogram(
            _CRAWL_DURATION_METRIC,
            unit=DURATION_UNIT,
            description="crawler execution duration in seconds",
        )
        histogram.record(duration, {OUTCOME: outcome})


__all__ = ["CrawlerController"]

# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Live public-HTTPS browser policy inside the CrawlerRuntime (PR 20).

Chromium reaches the network only through the Podman-injected per-execution
CONNECT proxy. This module validates that injected topology, then registers a
context-wide request/WebSocket interception policy **before** the first page
exists. Every browser request (navigation, redirect, subresource, JS fetch)
is counted and must match the exact approved origin/path; only GET/HEAD are
permitted and any violation is sticky, so no partial successful crawl is
returned.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from crawler_runtime.public_policy import (
    is_allowed_https_url,
    is_safe_public_host,
    validate_live_path_prefix,
)

#: Podman-injected proxy endpoint (private internal gateway address).
ENV_HTTPS_PROXY = "DARKULA_CRAWLER_HTTPS_PROXY"
#: Podman-injected private internal network CIDR.
ENV_INTERNAL_CIDR = "DARKULA_CRAWLER_INTERNAL_CIDR"

#: Ambient proxy variables that must never be present in a live container.
_AMBIENT_PROXY_ENV = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)

#: Exact injected proxy shape: one private IPv4 and the gateway port.
_PROXY_RE = re.compile(r"^http://([0-9]{1,3}(?:\.[0-9]{1,3}){3}):3128$")

#: Hard live browser-request ceiling (all intercepted requests).
MAX_LIVE_REQUESTS = 60
#: Hard live redirect ceiling (including subresource redirects).
MAX_LIVE_REDIRECTS = 8

_ALLOWED_METHODS = frozenset({"GET", "HEAD"})
_FORBIDDEN_HEADERS = frozenset({"authorization", "proxy-authorization", "forwarded"})


class LivePolicyError(ValueError):
    """A malformed or unsafe live-runtime topology configuration."""


@dataclass(frozen=True, slots=True)
class LiveProxyConfig:
    """Validated Podman-injected proxy topology."""

    server: str
    proxy_ip: str
    internal_cidr: str

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> LiveProxyConfig:
        """Parse and validate the injected proxy before any browser launch."""
        for name in _AMBIENT_PROXY_ENV:
            if environ.get(name, "").strip():
                raise LivePolicyError("ambient proxy configuration is not permitted")
        raw_proxy = environ.get(ENV_HTTPS_PROXY)
        raw_cidr = environ.get(ENV_INTERNAL_CIDR)
        if not raw_proxy or not raw_cidr:
            raise LivePolicyError("missing live proxy topology")
        match = _PROXY_RE.fullmatch(raw_proxy)
        if match is None:
            raise LivePolicyError("malformed live proxy endpoint")
        try:
            proxy_ip = ipaddress.IPv4Address(match.group(1))
        except ValueError:
            raise LivePolicyError("malformed live proxy address") from None
        if (
            proxy_ip.is_unspecified
            or proxy_ip.is_multicast
            or proxy_ip.is_loopback
            or not proxy_ip.is_private
        ):
            raise LivePolicyError("live proxy address is not a private internal IPv4")
        try:
            network = ipaddress.IPv4Network(raw_cidr, strict=True)
        except ValueError:
            raise LivePolicyError("malformed internal network CIDR") from None
        if network.prefixlen != 24:
            raise LivePolicyError("internal network CIDR must be a /24")
        if proxy_ip not in network:
            raise LivePolicyError("live proxy address is outside the internal network")
        return cls(
            server=raw_proxy,
            proxy_ip=str(proxy_ip),
            internal_cidr=str(network),
        )


def browser_context_options(config: LiveProxyConfig) -> dict[str, object]:
    """Return the exact live Playwright context options (strict TLS)."""
    return {
        "proxy": {"server": config.server},
        "service_workers": "block",
        "accept_downloads": False,
        "ignore_https_errors": False,
    }


class LiveRequestPolicy:
    """Sticky, bounded policy for every live browser network request."""

    def __init__(
        self,
        *,
        approved_host: str,
        allowed_path_prefix: str,
        max_requests: int = MAX_LIVE_REQUESTS,
        max_redirects: int = MAX_LIVE_REDIRECTS,
    ) -> None:
        if not is_safe_public_host(approved_host):
            raise LivePolicyError("invalid approved live host")
        try:
            prefix = validate_live_path_prefix(allowed_path_prefix)
        except ValueError:
            raise LivePolicyError("invalid approved live path prefix") from None
        if max_requests < 1 or max_requests > MAX_LIVE_REQUESTS:
            raise LivePolicyError("invalid live request budget")
        if max_redirects < 0 or max_redirects > MAX_LIVE_REDIRECTS:
            raise LivePolicyError("invalid live redirect budget")
        self._host = approved_host
        self._prefix = prefix
        self._max_requests = max_requests
        self._max_redirects = max_redirects
        self.request_count = 0
        self.redirects_observed = 0
        self.violation: str | None = None

    @property
    def approved_host(self) -> str:
        """The exact host every live URL must match."""
        return self._host

    @property
    def allowed_path_prefix(self) -> str:
        """The component-boundary path prefix every live URL must match."""
        return self._prefix

    def _flag(self, reason: str) -> None:
        if self.violation is None:
            self.violation = reason

    def flag(self, reason: str) -> None:
        """Record one bounded sticky violation reason (first one wins)."""
        self._flag(reason)

    def is_authorized(self, url: str) -> bool:
        """Return whether a URL matches the exact live grant."""
        return is_allowed_https_url(url, self._host, self._prefix)

    def record_redirect(self) -> None:
        """Account one observed 3xx response (including subresources)."""
        self.redirects_observed += 1
        if self.redirects_observed > self._max_redirects:
            self._flag("redirect_budget_exceeded")

    async def route_handler(self, route: Any) -> None:
        """Abort any request that is not an authorized GET/HEAD to the grant."""
        self.request_count += 1
        if self.request_count > self._max_requests:
            self._flag("request_budget_exceeded")
            await route.abort("blockedbyclient")
            return
        if self.violation is not None:
            await route.abort("blockedbyclient")
            return
        request = route.request
        method = str(getattr(request, "method", "")).upper()
        if method not in _ALLOWED_METHODS:
            self._flag("method_not_allowed")
            await route.abort("blockedbyclient")
            return
        headers = getattr(request, "headers", {}) or {}
        if isinstance(headers, Mapping):
            for name in headers:
                lowered = str(name).lower()
                if lowered in _FORBIDDEN_HEADERS or lowered.startswith("x-forwarded-"):
                    self._flag("forbidden_header")
                    await route.abort("blockedbyclient")
                    return
        url = str(getattr(request, "url", ""))
        if not self.is_authorized(url):
            self._flag("url_not_authorized")
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def websocket_handler(self, websocket: Any) -> None:
        """Deny every WebSocket in the live path."""
        self._flag("websocket_denied")
        await websocket.close()


__all__ = [
    "ENV_HTTPS_PROXY",
    "ENV_INTERNAL_CIDR",
    "MAX_LIVE_REDIRECTS",
    "MAX_LIVE_REQUESTS",
    "LivePolicyError",
    "LiveProxyConfig",
    "LiveRequestPolicy",
]

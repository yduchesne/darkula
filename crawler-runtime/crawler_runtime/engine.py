# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic bounded traversal engine (PR 7).

Browser-independent: the engine drives a :class:`PageAdapter` (a minimal
navigation abstraction) so the logic is unit-testable without Playwright
and the concrete adapter lives in :mod:`crawler_runtime.playwright_adapter`.

The engine is protocol-agnostic: :class:`TraversalInput`/:class:`TraversalOutput`
are the engine's own typed data; the entry point maps them to/from the
versioned controller-runtime protocol JSON at the boundary.

Traversal rules (deterministic):

- breadth-first over **sorted** discovered same-origin links;
- parsed-origin authorization: only the controller-provided
  ``scheme://host:port`` may ever be loaded;
- page/request/depth budgets and an internal time deadline are enforced in
  addition to the hard sandbox limits;
- source HTTP failures (429/503) become bounded observations, never
  infrastructure crashes; 503 gets exactly one bounded retry;
- login forms are submitted once with the granted credentials and the
  browser context keeps the resulting session;
- self-destructive endpoints are never navigated (``/logout``);
- fragments, non-http schemes, and out-of-origin URLs are dropped.

The output is bounded observations/links plus a bounded machine status; no
autonomous "interestingness" inference exists.
"""

from __future__ import annotations

import asyncio
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass
from typing import Protocol

#: Per-navigation browser timeout ceiling (seconds).
_PAGE_TIMEOUT_CEILING_SECONDS = 15.0

#: Bounded wait before one 503 retry (seconds); honors Retry-After up to this.
_MAX_RETRY_WAIT_SECONDS = 2.0

#: Exactly one retry for transient 503 responses (deterministic, bounded).
_MAX_503_RETRIES = 1

#: Navigational paths whose execution would destroy the crawl's own session.
_SELF_DESTRUCTIVE_PATHS = frozenset({"/logout"})

#: Allowed navigation schemes inside the granted origin.
_ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Bounded ceiling on discovered links returned by one execution.
_MAX_LINKS = 500


@dataclass(frozen=True, slots=True)
class TraversalCredentials:
    """Bounded source authentication material (execution-scoped)."""

    username: str
    password: str


@dataclass(frozen=True, slots=True)
class TraversalInput:
    """Engine input mirroring the controller-runtime protocol (schema v1)."""

    execution_id: str
    request_id: str
    start_url: str
    origin_scheme: str
    origin_host: str
    origin_port: int
    credentials: TraversalCredentials | None = None
    max_pages: int = 20
    max_requests: int = 60
    max_depth: int = 4
    timeout_seconds: float = 60.0
    max_page_text_bytes: int = 4096


@dataclass(frozen=True, slots=True)
class PageObservation:
    """One bounded observed page (hostile/untrusted data by convention)."""

    url: str
    depth: int
    http_status: int | None
    rendered: bool
    title: str
    text_excerpt: str


@dataclass(frozen=True, slots=True)
class TraversalOutput:
    """Engine output mirrored to the controller-runtime protocol (v1)."""

    execution_id: str
    request_id: str
    status: str
    pages: tuple[PageObservation, ...]
    links: tuple[str, ...]
    requests: int
    redirects_observed: int
    duration_seconds: float
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class NavigateResult:
    """One completed page navigation observed through the adapter."""

    url: str
    http_status: int | None
    title: str
    text: str
    links: tuple[str, ...]
    retry_after_seconds: float | None = None
    auth_form_present: bool = False


@dataclass(frozen=True, slots=True)
class AuthDecision:
    """Deterministic outcome of one optional login-form submission."""

    attempted: bool
    post_login_url: str | None = None
    post_login_status: int | None = None
    title: str = ""
    text: str = ""
    links: tuple[str, ...] = ()


class PageAdapter(Protocol):
    """Minimal navigation abstraction implemented by the Playwright adapter."""

    @property
    def redirects_observed(self) -> int:
        """Number of 3xx redirects the adapter observed this execution."""
        ...

    async def navigate(self, url: str, *, timeout_ms: int) -> NavigateResult | None:
        """Load one page and return its bounded observation.

        Returns ``None`` on a navigation-level failure (never an
        infrastructure crash); 429/503 statuses are returned as data.
        """
        ...

    async def submit_auth_form(
        self,
        *,
        username: str,
        password: str,
        timeout_ms: int,
    ) -> AuthDecision:
        """Fill and submit the current page's login form (if present)."""
        ...

    async def close(self) -> None:
        """Close the browser context (best effort, never raises)."""
        ...


def _is_within_origin(url: str, payload: TraversalInput) -> bool:
    """Return whether an absolute URL belongs to the granted origin."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme not in _ALLOWED_SCHEMES:
        return False
    host = (parts.hostname or "").lower()
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return (
        parts.scheme == payload.origin_scheme
        and host == payload.origin_host
        and port == payload.origin_port
    )


def _normalize_url(url: str, base: str) -> str | None:
    """Resolve a link against a base and return a canonical absolute URL.

    Returns ``None`` for fragments-only, non-http(s), or malformed links.
    """
    try:
        resolved = urllib.parse.urljoin(base, url.strip())
        parts = urllib.parse.urlsplit(resolved)
    except ValueError:
        return None
    if parts.scheme not in _ALLOWED_SCHEMES:
        return None
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, parts.query, "")
    )


def _bounded_text(text: str, *, max_bytes: int) -> str:
    """Return a byte-bounded, control-stripped text excerpt (data only)."""
    # The controller boundary rejects control characters outright; the runtime
    # must never emit them, so strip every C0/C1 control and DEL here.
    cleaned = "".join(ch for ch in text if 32 <= ord(ch) <= 126 or ord(ch) >= 160)
    encoded = cleaned.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return encoded.decode("utf-8", errors="replace")
    truncated = encoded[:max_bytes]
    # Cut trailing UTF-8 continuation bytes so the excerpt ends on a boundary.
    while truncated and (truncated[-1] & 0xC0) == 0x80:
        truncated = truncated[:-1]
    return truncated.decode("utf-8", errors="replace")


def _retry_wait(retry_after: float | None) -> float:
    """Return a bounded deterministic wait before a 503 retry."""
    if retry_after is None or retry_after <= 0:
        return 0.5
    return min(retry_after, _MAX_RETRY_WAIT_SECONDS)


async def traverse(
    adapter: PageAdapter,
    payload: TraversalInput,
) -> TraversalOutput:
    """Execute the deterministic bounded traversal and return its output."""
    start = time.monotonic()
    deadline = start + payload.timeout_seconds
    seen_urls: set[str] = set()
    enqueued: set[str] = set()
    discovered: list[str] = []
    seen_links: set[str] = set()
    observations: list[PageObservation] = []
    requests = 0
    status = "completed"
    reason: str | None = None
    logged_in = False
    reobserved_post_login: set[str] = set()

    def record_links(links: tuple[str, ...], base_url: str) -> None:
        for raw in links:
            absolute = _normalize_url(raw, base_url)
            if absolute is None or not _is_within_origin(absolute, payload):
                continue
            parts = urllib.parse.urlsplit(absolute)
            if parts.path in _SELF_DESTRUCTIVE_PATHS:
                continue
            if absolute in seen_links:
                continue
            seen_links.add(absolute)
            discovered.append(absolute)

    def budget_exhausted() -> bool:
        return (
            len(observations) >= payload.max_pages or requests >= payload.max_requests
        )

    queue: deque[tuple[str, int]] = deque()
    queue.append((payload.start_url.strip(), 0))
    try:
        while queue and not budget_exhausted():
            if time.monotonic() >= deadline:
                status = "timed_out"
                reason = "runtime time budget exhausted"
                break
            url, depth = queue.popleft()
            if url in seen_urls:
                continue
            seen_urls.add(url)
            if depth > payload.max_depth:
                continue
            remaining = max(0.001, deadline - time.monotonic())
            timeout_ms = int(min(_PAGE_TIMEOUT_CEILING_SECONDS, remaining) * 1000)
            redirects_before = adapter.redirects_observed
            result = await adapter.navigate(url, timeout_ms=timeout_ms)
            requests += 1
            nav_had_redirect = adapter.redirects_observed > redirects_before
            if result is None:
                observations.append(
                    PageObservation(
                        url=url,
                        depth=depth,
                        http_status=None,
                        rendered=False,
                        title="",
                        text_excerpt="",
                    )
                )
            else:
                # Source-level 503: exactly one bounded retry.
                if (
                    result.http_status == 503
                    and requests < payload.max_requests
                    and _MAX_503_RETRIES >= 1
                ):
                    await asyncio.sleep(_retry_wait(result.retry_after_seconds))
                    retried = await adapter.navigate(url, timeout_ms=timeout_ms)
                    requests += 1
                    if retried is not None:
                        result = retried
                observations.append(
                    PageObservation(
                        url=url,
                        depth=depth,
                        http_status=result.http_status,
                        rendered=result.http_status is not None,
                        title=_bounded_text(
                            result.title, max_bytes=payload.max_page_text_bytes
                        ),
                        text_excerpt=_bounded_text(
                            result.text, max_bytes=payload.max_page_text_bytes
                        ),
                    )
                )
                record_links(result.links, url)

                # Deterministic optional login: submit the form once.
                if (
                    result.auth_form_present
                    and not logged_in
                    and payload.credentials is not None
                ):
                    decision = await adapter.submit_auth_form(
                        username=payload.credentials.username,
                        password=payload.credentials.password,
                        timeout_ms=timeout_ms,
                    )
                    requests += 1
                    if decision.attempted and decision.post_login_url is not None:
                        logged_in = True
                        post_url = decision.post_login_url
                        seen_urls.add(post_url)
                        observations.append(
                            PageObservation(
                                url=post_url,
                                depth=depth + 1,
                                http_status=decision.post_login_status,
                                rendered=decision.post_login_status is not None,
                                title=_bounded_text(
                                    decision.title,
                                    max_bytes=payload.max_page_text_bytes,
                                ),
                                text_excerpt=_bounded_text(
                                    decision.text,
                                    max_bytes=payload.max_page_text_bytes,
                                ),
                            )
                        )
                        record_links(decision.links, post_url)
                        if budget_exhausted():
                            break
                        # The intended URL was bounced to the login form and the
                        # form landed elsewhere (e.g. a session index): retry the
                        # original target once now that the session is valid so
                        # protected content is actually observed, not just the
                        # bounce and the login destination.
                        if (
                            post_url != url
                            and nav_had_redirect
                            and url not in reobserved_post_login
                            and requests < payload.max_requests
                            and len(observations) < payload.max_pages
                        ):
                            reobserved_post_login.add(url)
                            retried_target = await adapter.navigate(
                                url, timeout_ms=timeout_ms
                            )
                            requests += 1
                            if retried_target is not None:
                                observations.append(
                                    PageObservation(
                                        url=url,
                                        depth=depth + 1,
                                        http_status=retried_target.http_status,
                                        rendered=retried_target.http_status is not None,
                                        title=_bounded_text(
                                            retried_target.title,
                                            max_bytes=payload.max_page_text_bytes,
                                        ),
                                        text_excerpt=_bounded_text(
                                            retried_target.text,
                                            max_bytes=payload.max_page_text_bytes,
                                        ),
                                    )
                                )
                                record_links(retried_target.links, url)
                        if budget_exhausted():
                            break

            # Enqueue newly discovered links within the depth budget.
            next_depth = depth + 1
            for link in sorted(discovered):
                if link in seen_urls or link in enqueued:
                    continue
                if next_depth > payload.max_depth:
                    continue
                if len(seen_urls) + len(enqueued) >= payload.max_pages + _MAX_LINKS:
                    break
                enqueued.add(link)
                queue.append((link, next_depth))
    except asyncio.CancelledError:
        raise
    except Exception:
        status = "workload_failed"
        reason = "browser workload failed (bounded)"

    return TraversalOutput(
        execution_id=payload.execution_id,
        request_id=payload.request_id,
        status=status,
        pages=tuple(observations),
        links=tuple(discovered[:_MAX_LINKS]),
        requests=requests,
        redirects_observed=adapter.redirects_observed,
        duration_seconds=time.monotonic() - start,
        reason=reason,
    )


__all__ = [
    "AuthDecision",
    "NavigateResult",
    "PageAdapter",
    "PageObservation",
    "TraversalCredentials",
    "TraversalInput",
    "TraversalOutput",
    "traverse",
]

# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Playwright + Chromium adapter inside the CrawlerRuntime (PR 7).

The only place in the whole Darkula codebase that drives Playwright: the
trusted side never imports this module and the browser is never controlled
outside a disposable sandbox container. Chromium runs without
``--no-sandbox`` as a non-root user under read-only rootfs/capability/resource
limitations (verified experimentally; see docs/SECURITY.md).

The adapter keeps one page and a fresh context per execution, counts observed
3xx redirects through a response listener, reads Retry-After for 503s, and
turns every navigation-level failure into ``None``/bounded data rather than
raising (source HTTP failures are never infrastructure crashes).
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any

from crawler_runtime.engine import AuthDecision, NavigateResult

if TYPE_CHECKING:
    from playwright.async_api import Page

#: Bounded excerpt length taken from ``innerText`` before the engine bounds it.
_MAX_ADAPTER_TEXT_CHARS = 20000

#: Grace period (ms) for the download event to settle after "Download is starting".
_DOWNLOAD_EVENT_GRACE_MS = 5000

#: Download-event polling budget after a "Download is starting" navigation.
_DOWNLOAD_POLL_TRIES = 100
_DOWNLOAD_POLL_INTERVAL = 0.05


class PlaywrightPageAdapter:
    """Playwright-backed :class:`PageAdapter` for one navigation context."""

    def __init__(self, page: Page) -> None:
        self._page = page
        self._redirects: int = 0
        self._pending_download: Any = None
        page.on("response", self._on_response)
        page.on("download", self._on_download)

    @property
    def redirects_observed(self) -> int:
        """Number of 3xx redirect responses observed this execution."""
        return self._redirects

    def _on_response(self, response: Any) -> None:
        status = getattr(response, "status", None)
        if isinstance(status, int) and status in (301, 302, 303, 307, 308):
            self._redirects += 1

    def _on_download(self, download: Any) -> None:
        """Grab the latest Content-Disposition download for this execution."""
        self._pending_download = download

    async def _current_observation(self, url: str) -> NavigateResult:
        """Extract a bounded observation from the currently loaded page."""
        try:
            title = await self._page.title()
            text = await self._page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            )
            raw_links = await self._page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]')).map("
                "a => a.getAttribute('href'))"
            )
            auth_form = await self._page.evaluate(
                "() => Array.from(document.querySelectorAll('form')).some("
                "f => f.querySelector('input[type=password]') !== null)"
            )
        except Exception:
            return NavigateResult(
                url=url, http_status=None, title="", text="", links=()
            )
        links = tuple(
            str(raw) for raw in raw_links if isinstance(raw, str) and raw.strip()
        )
        return NavigateResult(
            url=url,
            http_status=None,  # filled by the caller after response handling
            title=title[:_MAX_ADAPTER_TEXT_CHARS],
            text=text[:_MAX_ADAPTER_TEXT_CHARS],
            links=links,
            auth_form_present=bool(auth_form),
        )

    async def _sample_download(self, download: Any) -> str:
        """Return a bounded, control-stripped sample of a downloaded artifact.

        The runtime has no ObjectStore: hostile download bodies must never be
        buffered unboundedly or echoed. The sandbox tmpfs caps the download
        temp file (container resource ceiling); here only the first few bytes
        are sampled as data and the temp file is then discarded.
        """
        try:
            path = await download.path()
        except Exception:
            return ""
        sample = b""
        try:
            with open(path, "rb") as handle:  # noqa: ASYNC230 - bounded tmpfs sample
                sample = handle.read(_MAX_ADAPTER_TEXT_CHARS)
        except Exception:
            return ""
        try:
            os.remove(path)
        except Exception:
            pass
        return sample.decode("utf-8", errors="replace")

    async def navigate(self, url: str, *, timeout_ms: int) -> NavigateResult | None:
        """Load one URL and return its bounded observation (or ``None``)."""
        last_response: object | None = None
        exception_message = ""
        try:
            last_response = await self._page.goto(
                url, wait_until="domcontentloaded", timeout=timeout_ms
            )
        except Exception as exc:
            exception_message = str(exc)
        # Content-Disposition: attachment responses never render a page. When
        # the navigation raises "Download is starting" the download event fires
        # right after; poll briefly for it, then record a bounded sample.
        download: Any | None = self._pending_download
        self._pending_download = None
        if download is None and "Download is starting" in exception_message:
            for _ in range(_DOWNLOAD_POLL_TRIES):
                await asyncio.sleep(_DOWNLOAD_POLL_INTERVAL)
                download = self._pending_download
                self._pending_download = None
                if download is not None:
                    break
        if download is not None:
            try:
                failed = await download.failure()
            except Exception:
                failed = "inspect-failed"
            if not failed:
                sample = await self._sample_download(download)
                return NavigateResult(
                    url=url, http_status=200, title="", text=sample, links=()
                )
            return NavigateResult(
                url=url, http_status=None, title="", text="", links=()
            )
        try:
            observation = await self._current_observation(url)
        except Exception:
            return None
        status: int | None = None
        retry_after: float | None = None
        if last_response is not None:
            raw_status = getattr(last_response, "status", None)
            if isinstance(raw_status, int):
                status = raw_status
            raw_headers: Any = getattr(last_response, "headers", None)
            headers = raw_headers if isinstance(raw_headers, dict) else {}
            if isinstance(headers, dict):
                raw_retry = headers.get("retry-after")
                if isinstance(raw_retry, str) and raw_retry.isdigit():
                    retry_after = float(raw_retry)
        return NavigateResult(
            url=url,
            http_status=status,
            title=observation.title,
            text=observation.text,
            links=observation.links,
            retry_after_seconds=retry_after,
            auth_form_present=observation.auth_form_present,
        )

    async def submit_auth_form(
        self,
        *,
        username: str,
        password: str,
        timeout_ms: int,
    ) -> AuthDecision:
        """Fill and submit the current page's login form (if present)."""
        try:
            form = self._page.locator("form").filter(
                has=self._page.locator("input[type=password]")
            )
            if await form.count() == 0:
                return AuthDecision(attempted=False)
            await form.locator("input[type=text]").first.fill(username)
            await form.locator("input[type=password]").first.fill(password)
            async with self._page.expect_navigation(
                wait_until="domcontentloaded", timeout=timeout_ms
            ) as nav_info:
                await form.locator(
                    "button[type=submit], input[type=submit]"
                ).first.click()
            response = await nav_info.value
            post_status: int | None = None
            if response is not None:
                raw_status = getattr(response, "status", None)
                if isinstance(raw_status, int):
                    post_status = raw_status
            observation = await self._current_observation(self._page.url)
            return AuthDecision(
                attempted=True,
                post_login_url=self._page.url,
                post_login_status=post_status,
                title=observation.title,
                text=observation.text,
                links=observation.links,
            )
        except Exception:
            # A failed login must not crash the workload; the session
            # simply remains unauthenticated and traversal continues.
            return AuthDecision(attempted=False)

    async def close(self) -> None:
        """Close the page (best effort; never raises)."""
        try:
            await self._page.close()
        except Exception:
            pass


__all__ = ["PlaywrightPageAdapter"]

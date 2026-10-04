# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""CrawlerRuntime entry point (PR 7).

Runs inside the disposable crawler container: reads the bounded protocol
input from stdin, maps it to the traversal engine, drives Playwright +
Chromium deterministically, maps the engine output back to the protocol, and
writes the bounded output to stdout. Exit codes:

- ``0``: a valid protocol output document was produced (any runtime status);
- ``2``: the input could not be validated (protocol error / oversized).

The workload never exits with code 0 without a parseable output document.
Failures are bounded and typed; no credentials, cookies, or hostile content
are ever logged. The protocol module is copied into the image at build time
(``/app/runtime_protocol.py``) so both sides share one schema by
construction.
"""

from __future__ import annotations

import asyncio
import sys
import time

from crawler_runtime.engine import (
    TraversalCredentials,
    TraversalInput,
    TraversalOutput,
    traverse,
)
from crawler_runtime.playwright_adapter import PlaywrightPageAdapter

sys.path.insert(0, "/app")
from runtime_protocol import (
    PROTOCOL_VERSION,
    RuntimeInput,
    RuntimeOutput,
    RuntimeOutputPage,
    RuntimeProtocolError,
    decode_input,
    encode_output,
)

#: Bounded stdin read ceiling (matches the trusted input policy default).
_MAX_INPUT_BYTES = 65536
#: Bounded stdout write ceiling; the trusted side enforces the same bound.
_MAX_OUTPUT_BYTES = 1024 * 1024

#: Process start instant for bounded duration reporting on failures.
_STARTED = time.monotonic()


def _to_traversal_input(payload: RuntimeInput) -> TraversalInput:
    """Map the decoded protocol input onto the engine's typed input."""
    credentials = None
    if payload.credentials is not None:
        username = payload.credentials.username
        password = payload.credentials.password
        credentials = TraversalCredentials(username=username, password=password)
    return TraversalInput(
        execution_id=payload.execution_id,
        request_id=payload.request_id,
        start_url=payload.start_url,
        origin_scheme=payload.origin_scheme,
        origin_host=payload.origin_host,
        origin_port=payload.origin_port,
        credentials=credentials,
        max_pages=payload.max_pages,
        max_requests=payload.max_requests,
        max_depth=payload.max_depth,
        timeout_seconds=payload.timeout_seconds,
        max_page_text_bytes=payload.max_page_text_bytes,
    )


def _to_runtime_output(output: TraversalOutput) -> RuntimeOutput:
    """Map the engine output onto the versioned protocol output document."""
    pages = tuple(
        RuntimeOutputPage(
            url=page.url,
            depth=page.depth,
            http_status=page.http_status,
            rendered=page.rendered,
            title=page.title,
            text_excerpt=page.text_excerpt,
        )
        for page in output.pages
    )
    return RuntimeOutput(
        version=PROTOCOL_VERSION,
        execution_id=output.execution_id,
        request_id=output.request_id,
        status=output.status,
        pages=pages,
        links=output.links,
        requests=output.requests,
        redirects_observed=output.redirects_observed,
        duration_seconds=output.duration_seconds,
        reason=output.reason,
    )


def _fit_output(output: RuntimeOutput, *, limit: int) -> RuntimeOutput:
    """Deterministically trim a too-large output document to the bound."""
    if len(encode_output(output)) <= limit:
        return output
    shortened = tuple(
        RuntimeOutputPage(
            url=page.url[:256],
            depth=page.depth,
            http_status=page.http_status,
            rendered=page.rendered,
            title=page.title[:512],
            text_excerpt=page.text_excerpt[:512],
        )
        for page in output.pages[:40]
    )
    candidate: RuntimeOutput = RuntimeOutput(
        version=output.version,
        execution_id=output.execution_id,
        request_id=output.request_id,
        status="resource_limited",
        pages=shortened,
        links=output.links[:100],
        requests=output.requests,
        redirects_observed=output.redirects_observed,
        duration_seconds=output.duration_seconds,
        reason="runtime output size limit enforced",
    )
    if len(encode_output(candidate)) > limit:
        candidate = RuntimeOutput(
            version=output.version,
            execution_id=output.execution_id,
            request_id=output.request_id,
            status="resource_limited",
            pages=(),
            links=(),
            requests=output.requests,
            redirects_observed=output.redirects_observed,
            duration_seconds=output.duration_seconds,
            reason="runtime output size limit enforced",
        )
    return candidate


async def _run(payload: RuntimeInput) -> TraversalOutput:
    """Launch Chromium, traverse, and return the engine output."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            context = await browser.new_context()
            page = await context.new_page()
            adapter = PlaywrightPageAdapter(page)
            try:
                return await traverse(adapter, _to_traversal_input(payload))
            finally:
                await adapter.close()
        finally:
            await browser.close()


async def main() -> int:
    """Entry point: bounded stdin -> typed output on stdout."""
    raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES)
    try:
        payload = decode_input(raw, max_bytes=_MAX_INPUT_BYTES)
    except RuntimeProtocolError as exc:
        # Bounded categorization only; never the malformed payload content.
        sys.stderr.write(f"runtime input rejected: {exc}\n")
        return 2
    try:
        output = await _run(payload)
    except asyncio.CancelledError:
        raise
    except Exception:
        output = TraversalOutput(
            execution_id=payload.execution_id,
            request_id=payload.request_id,
            status="workload_failed",
            pages=(),
            links=(),
            requests=0,
            redirects_observed=0,
            duration_seconds=time.monotonic() - _STARTED,
            reason="browser workload failed (bounded)",
        )
    encoded = encode_output(
        _fit_output(_to_runtime_output(output), limit=_MAX_OUTPUT_BYTES)
    )
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:  # pragma: no cover - defensive
        raise SystemExit(143) from None

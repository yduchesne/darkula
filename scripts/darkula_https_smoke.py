#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""One-shot, fully opt-in public HTTPS smoke crawl (PR 20).

Requires BOTH human gates (``--live`` and ``--operator-approved``) before any
Podman initialization, then builds the ordinary ``PublicHttpsGrant`` /
``CrawlRequest`` and runs the existing ``CrawlerController`` over the existing
``PodmanSandbox`` exactly once.

Output is deliberately counter-only: status, page/request counts, duration,
one fixed reason code and cleanup evidence. It never prints URLs, hostnames,
page content, headers, cookies, DNS answers or credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from darkula.config.loader import load_settings
from darkula.crawler.contracts import (
    AllowedOrigin,
    CrawlRequest,
    CrawlStatus,
    InvalidCrawlRequest,
    PublicHttpsGrant,
)
from darkula.crawler.controller import CrawlerController
from darkula.infrastructure.sandbox.podman import PodmanSandbox

_LIVE_MAX_PAGES = 20
_LIVE_MAX_REQUESTS = 60
_LIVE_MAX_DEPTH = 2
_LIVE_MAX_TIMEOUT_SECONDS = 90.0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="One-shot operator-approved public HTTPS smoke crawl (PR 20)."
    )
    parser.add_argument("--live", action="store_true", help="required live opt-in")
    parser.add_argument(
        "--operator-approved",
        action="store_true",
        help="required human authorization for the chosen public target",
    )
    parser.add_argument("--start-url", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--path-prefix", default="/")
    parser.add_argument("--max-pages", type=int, default=2)
    parser.add_argument("--max-requests", type=int, default=10)
    parser.add_argument("--max-depth", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    return parser


def _within_live_limits(args: argparse.Namespace) -> bool:
    return (
        1 <= args.max_pages <= _LIVE_MAX_PAGES
        and 1 <= args.max_requests <= _LIVE_MAX_REQUESTS
        and 0 <= args.max_depth <= _LIVE_MAX_DEPTH
        and 1.0 <= args.timeout_seconds <= _LIVE_MAX_TIMEOUT_SECONDS
    )


def _print_result(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True))


async def _execute(args: argparse.Namespace) -> int:
    try:
        settings = load_settings()
    except Exception:
        sys.stderr.write("error: configuration could not be loaded\n")
        return 2
    crawler_settings = settings.crawler
    if (
        not crawler_settings.real_https_enabled
        or not crawler_settings.real_https_allowed_origins
    ):
        sys.stderr.write(
            "error: public HTTPS crawling is not enabled with an exact allowlist\n"
        )
        return 2
    try:
        origin = AllowedOrigin.parse(args.origin)
        grant = PublicHttpsGrant(origin=origin, allowed_path_prefix=args.path_prefix)
        request = CrawlRequest(
            request_id="https-smoke",
            start_url=args.start_url,
            allowed_origin=origin,
            credentials=None,
            max_pages=args.max_pages,
            max_requests=args.max_requests,
            max_depth=args.max_depth,
            timeout_seconds=args.timeout_seconds,
            public_https_grant=grant,
        )
    except InvalidCrawlRequest:
        sys.stderr.write("error: the requested public HTTPS grant is invalid\n")
        return 2

    sandbox = PodmanSandbox.from_settings(crawler_settings)
    controller = CrawlerController(settings=crawler_settings, sandbox=sandbox)
    try:
        result = await controller.crawl(request)
    except InvalidCrawlRequest:
        _print_result(
            {
                "status": "rejected",
                "reason_code": "request_not_authorized",
            }
        )
        return 1
    _print_result(
        {
            "status": result.status.value,
            "pages": len(result.pages),
            "requests": result.requests,
            "duration": round(result.duration_seconds, 3),
            "reason_code": result.reason or "none",
            "cleanup_verified": result.cleanup_succeeded is True,
        }
    )
    return 0 if result.status is CrawlStatus.COMPLETED else 1


def main(argv: list[str] | None = None) -> int:
    """Validate the two human gates before any Podman work."""
    args = _build_parser().parse_args(argv)
    if not args.live or not args.operator_approved:
        sys.stderr.write(
            "error: live HTTPS smoke requires --live and --operator-approved\n"
        )
        return 2
    if not _within_live_limits(args):
        sys.stderr.write("error: live HTTPS smoke budget exceeds the hard limit\n")
        return 2
    return asyncio.run(_execute(args))


if __name__ == "__main__":
    raise SystemExit(main())

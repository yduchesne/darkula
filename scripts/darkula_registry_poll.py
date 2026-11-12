#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Single-shot, opt-in RansomLook registry import (PR 18).

Usage:

    uv run python scripts/darkula_registry_poll.py --provider ransomlook --live

This is **not** a scheduler/daemon/web API. It performs exactly one bounded
poll pass against the production PostgreSQL persistence boundary and exits.

Live network is triple-gated: ``--live`` **and**
``registry.ransomlook_enabled=true`` **and**
``registry.ransomlook_live_enabled=true``. Missing any gate exits ``2`` with
zero network requests. There is no in-memory fallback in this path.

RansomLook registry data are third-party claims made available by their
contributors under CC BY 4.0; they are not independently verified facts.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from darkula.app.persistence import ConflictError, PersistenceError
from darkula.app.registry_polling import (
    RegistryPoller,
    RegistryPollOutcome,
    RegistryRateLimited,
    RegistrySchemaError,
)
from darkula.app.source_discovery import SourceDiscoveryService
from darkula.config.loader import load_settings
from darkula.infrastructure.discovery.http import StdlibRegistryHttpTransport
from darkula.infrastructure.discovery.ransomlook import RansomLookDiscoveryAdapter
from darkula.infrastructure.persistence.postgresql.spi import PostgresDarkulaSpi

SUPPORTED_PROVIDERS = ("ransomlook",)

EXIT_OK = 0
EXIT_DISABLED = 2
EXIT_TEMPORARY = 3
EXIT_CONFLICT = 4
EXIT_CANCELLED = 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run exactly one bounded registry poll pass. Live access is "
            "explicitly opt-in and default-off."
        )
    )
    parser.add_argument(
        "--provider",
        required=True,
        choices=SUPPORTED_PROVIDERS,
        help="allowlisted registry provider (PR 18: ransomlook only)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "instantiate the production HTTP transport (also requires both "
            "registry enable flags)"
        ),
    )
    return parser


def _exit_for_outcome(outcome: RegistryPollOutcome) -> int:
    if outcome in (RegistryPollOutcome.COMPLETED, RegistryPollOutcome.COOLDOWN):
        return EXIT_OK
    if outcome is RegistryPollOutcome.CHECKPOINT_RACE:
        return EXIT_CONFLICT
    if outcome is RegistryPollOutcome.TEMPORARY_ERROR:
        return EXIT_TEMPORARY
    return EXIT_DISABLED


async def _run(provider: str) -> int:
    settings = load_settings()
    registry = settings.registry
    if not (registry.ransomlook_enabled and registry.ransomlook_live_enabled):
        print(
            "registry poll disabled: both ransomlook_enabled and "
            "ransomlook_live_enabled must be true",
            file=sys.stderr,
        )
        return EXIT_DISABLED

    adapter = RansomLookDiscoveryAdapter(
        transport=StdlibRegistryHttpTransport(),
        max_index_entries=registry.max_index_entries,
        max_locations_per_group=registry.max_locations_per_group,
        max_response_bytes=registry.max_response_bytes,
        request_timeout_seconds=registry.request_timeout_seconds,
        request_interval_seconds=registry.request_interval_seconds,
    )
    spi = PostgresDarkulaSpi.from_settings(settings.database)
    service = SourceDiscoveryService(persistence=spi)
    poller = RegistryPoller(
        persistence=spi,
        discovery_service=service,
        clock=lambda: datetime.now(UTC),
    )
    async with spi.lifecycle():
        result = await poller.poll_once(
            adapter, max_entries=registry.max_entries_per_run
        )
    print(
        "registry poll "
        f"provider={result.provider_id} "
        f"outcome={result.outcome.value} "
        f"entries_selected={result.entries_selected} "
        f"entries_completed={result.entries_completed} "
        f"observations_added={result.observations_added} "
        f"observations_replayed={result.observations_replayed} "
        f"records_skipped={result.records_skipped} "
        f"checkpoint_generation={result.checkpoint_generation}"
    )
    del provider
    return _exit_for_outcome(result.outcome)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.provider not in SUPPORTED_PROVIDERS:
        print("unsupported registry provider", file=sys.stderr)
        return EXIT_DISABLED
    if not args.live:
        print(
            "registry poll disabled: --live is required for live network access",
            file=sys.stderr,
        )
        return EXIT_DISABLED
    try:
        return asyncio.run(_run(args.provider))
    except KeyboardInterrupt:
        return EXIT_CANCELLED
    except asyncio.CancelledError:
        return EXIT_CANCELLED
    except RegistryRateLimited:
        print("registry poll rate limited", file=sys.stderr)
        return EXIT_TEMPORARY
    except RegistrySchemaError:
        print("registry poll rejected an invalid provider schema", file=sys.stderr)
        return EXIT_DISABLED
    except ConflictError:
        print("registry poll detected an immutable record conflict", file=sys.stderr)
        return EXIT_CONFLICT
    except PersistenceError:
        print("registry poll could not use durable persistence", file=sys.stderr)
        return EXIT_DISABLED


if __name__ == "__main__":
    raise SystemExit(main())

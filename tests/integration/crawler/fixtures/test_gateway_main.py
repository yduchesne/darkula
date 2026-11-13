# SPDX-License-Identifier: AGPL-3.0-only
"""Test-only CONNECT gateway entry point (PR 20 integration fixtures).

Reuses the real production parser/policy/gateway but injects a fail-closed
resolver and dialer so the integration suite can prove topology, CONNECT
denial and cleanup without ever issuing a public DNS or TCP request.

This module is copied into a **test-only** image; the production gateway
image never contains this override entry point.
"""

from __future__ import annotations

import asyncio
import os

from https_egress.gateway import HttpsEgressGateway
from https_egress.policy import EgressDenied, GatewayConfig


async def _deny_dns(host: str) -> str:
    raise EgressDenied("dns_failed")


async def _deny_dial(
    address: str, port: int, timeout_seconds: float
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    raise OSError("test gateway never dials the public network")


async def _serve(config: GatewayConfig) -> None:
    gateway = HttpsEgressGateway(config, resolver=_deny_dns, connector=_deny_dial)
    await gateway.start()
    await gateway.serve()


def main() -> int:
    try:
        config = GatewayConfig.from_environment(dict(os.environ))
    except ValueError:
        return 2
    asyncio.run(_serve(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

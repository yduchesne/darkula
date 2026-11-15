# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""HTTPS egress gateway process entry point (PR 20).

Reads only the Podman-injected topology variables, validates them before
binding a listener, serves the single approved FQDN:443 CONNECT capability,
and closes every listener/tunnel at the bounded execution deadline or on
SIGTERM/SIGINT.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from https_egress.gateway import HttpsEgressGateway
from https_egress.policy import GatewayConfig

_LOGGER = logging.getLogger("https_egress")


async def _serve(config: GatewayConfig) -> None:
    gateway = HttpsEgressGateway(config)
    await gateway.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX platform
            pass
    serve_task = asyncio.create_task(gateway.serve(), name="egress-serve")
    stop_task = asyncio.create_task(stop.wait(), name="egress-stop")
    try:
        await asyncio.wait({serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (serve_task, stop_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(serve_task, stop_task, return_exceptions=True)
        await gateway.close()


def main() -> int:
    """Run the gateway; exit 2 on invalid configuration (never bind)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )
    try:
        config = GatewayConfig.from_environment(dict(os.environ))
    except ValueError:
        _LOGGER.error("gateway configuration rejected")
        return 2
    try:
        asyncio.run(_serve(config))
    except KeyboardInterrupt:  # pragma: no cover - defensive
        return 143
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

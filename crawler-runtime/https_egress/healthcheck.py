# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""In-container readiness probe for the HTTPS egress gateway (PR 20).

Connects only to the gateway's own configured internal listener. It performs
no DNS lookup and no public network interaction; the trusted controller runs
it through ``podman exec`` before launching the crawler.
"""

from __future__ import annotations

import os
import socket
import sys

from https_egress.policy import ENV_BIND_IPV4, ENV_LISTEN_PORT

_TIMEOUT_SECONDS = 3.0


def main() -> int:
    """Return 0 when the gateway listener accepts its private address."""
    address = os.environ.get(ENV_BIND_IPV4, "")
    raw_port = os.environ.get(ENV_LISTEN_PORT, "")
    if not address or not raw_port.isdigit():
        return 2
    try:
        with socket.create_connection((address, int(raw_port)), _TIMEOUT_SECONDS):
            return 0
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())

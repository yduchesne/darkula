# SPDX-FileCopyrightText: 2026 Darkula contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Fake World HTTP serve entry point (PR 7, extended PR 15).

Runs the canonical ``darkula-cross-source`` scenario adapter on the
Darkula-owned internal network service port 8080 (no host publishing;
container-to-container only, per the prefix-3 host-port rules). The container
hostname defaults to the canonical BlackGate source, and the PR 15 source
hostnames dispatch to their archetype through the optional host map. Quiet by
design: hostile request lines never enter logs.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, "/app")

from darkula.testing.fake_world.http_adapter import (
    FakeWorldHttpServer,
    FakeWorldHttpService,
)
from darkula.testing.fake_world.rendering import CrossSourceRenderer

HOST = "0.0.0.0"
PORT = int(os.environ.get("DARKULA_FAKEWORLD_PORT", "8080"))

#: Virtual-host dispatch for the PR 15 cross-source scenario. The container's
#: own hostname is not mapped and therefore serves the canonical BlackGate
#: source by default (identical to the PR 6/7 behavior).
_HOST_MAP = {
    "blackgate.example.test": "blackgate",
    "accessbay.example.test": "accessbay",
    "nightleak.example.test": "nightleak",
    "shadowtalk.example.test": "shadowtalk",
}


def main() -> int:
    service = FakeWorldHttpService(
        CrossSourceRenderer(),
        scenario_id="darkula-cross-source",
        version=1,
        source_id="blackgate",
        host_map=_HOST_MAP,
    )
    server = FakeWorldHttpServer(HOST, PORT, service)
    print(
        f"darkula-fakeworld: cross-source (BlackGate default) HTTP on {HOST}:{PORT}",
        flush=True,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
